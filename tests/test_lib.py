import os, sys, unittest
from urllib.parse import unquote
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import onboarding_lib as lib

D = "wh_padrao"
PRIV = ["USE_SCHEMA", "SELECT", "MODIFY", "CREATE_TABLE", "CREATE_FUNCTION", "CREATE_MATERIALIZED_VIEW", "EXECUTE", "REFRESH"]
ENT = ["WORKSPACE_ACCESS", "DATABRICKS_SQL_ACCESS"]
WH = {"tamanho": "2X-Small", "min_clusters": 1, "max_clusters": 3, "auto_stop_min": 5}


class FakeApi:
    """Simula as APIs: guarda chamadas e responde GETs a partir de um estado em memória."""
    def __init__(self, groups=(), users=(), assigned=(), schemas=(), warehouses=(), services=(), members=()):
        self.members = set(members)
        self.service_limits = []
        self.groups, self.users = dict(groups), dict(users)
        self.assigned, self.schemas, self.services = set(assigned), set(schemas), set(services)
        self.warehouses = list(warehouses)
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        path = unquote(path)
        if method == "GET" and path.startswith("/api/2.0/identity/groups?"):
            name = path.split('"')[1]
            return {"groups": [{"group_id": self.groups[name]}]} if name in self.groups else {}
        if method == "GET" and path.startswith("/api/2.0/identity/users?"):
            name = path.split('"')[1]
            return {"users": [{"user_id": self.users[name]}]} if name in self.users else {}
        if method == "GET" and "/direct-members" in path:
            gid = path.split("/groups/")[1].split("/")[0]
            return {"direct_group_members": [{"principal_id": int(u)} for g, u in self.members if g == gid]}
        if method == "GET" and path.startswith("/api/2.0/identity/workspace-assignments/"):
            if path.rsplit("/", 1)[1] in self.assigned:
                return {}
            raise lib.ApiError("not directly assigned", status=404)
        if method == "GET" and path.startswith("/api/2.1/unity-catalog/schemas/"):
            if path.rsplit("/", 1)[1] in self.schemas:
                return {}
            raise lib.ApiError("x", status=404)
        if method == "GET" and path.startswith("/api/2.1/unity-catalog/model-services/"):
            if path.rsplit("/", 1)[1] in self.services:
                return {"config": {"rate_limits": self.service_limits}}
            raise lib.ApiError("x", error_code="NOT_FOUND")
        if method == "GET" and path == "/api/2.0/sql/warehouses":
            return {"warehouses": self.warehouses}
        if method == "GET" and path.startswith("/api/2.0/sql/warehouses/"):
            wid = path.rsplit("/", 1)[1]
            return next(dict(w) for w in self.warehouses if w["id"] == wid)
        if method == "POST" and path == "/api/2.0/identity/groups":
            return {"group_id": "101"}
        if method == "POST" and path == "/api/2.0/identity/users":
            return {"user_id": "202"}
        if method == "POST" and path == "/api/2.0/sql/warehouses":
            return {"id": "wh-new"}
        return {}

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


class TestPasta(unittest.TestCase):
    def _api(self, acl):
        def api(m, p, b=None):
            return {"object_id": 7} if "get-status" in p else {"access_control_list": acl}
        return api

    def test_pasta_pessoal_ok(self):
        acl = [{"user_name": "admin@x.com"}, {"group_name": "admins"}]
        self.assertEqual(lib.broad_access_on_folder(self._api(acl), "/Workspace/Users/admin@x.com/o"), [])

    def test_pasta_shared_bloqueia(self):
        acl = [{"group_name": "users"}, {"group_name": "admins"}]
        self.assertEqual(lib.broad_access_on_folder(self._api(acl), "/Workspace/Shared/o"), ["users"])


class TestCsv(unittest.TestCase):
    def test_parse_ok_e_defaults(self):
        txt = "grupo,emails,schema,warehouse,modelos,tpm,qpm\nfin,a@x.com; B@X.com ,financas,,gpt-5-mini;gte,,\n"
        grupos, erros = lib.parse_csv(txt, D)
        self.assertEqual(erros, [])
        g = grupos[0]
        self.assertEqual(g["emails"], ["a@x.com", "b@x.com"])
        self.assertEqual(g["modelos"], ["gpt-5-mini", "gte"])
        self.assertEqual((g["warehouse"], g["tpm"], g["qpm"]), ("wh_padrao", None, None))

    def test_parse_erros(self):
        txt = "grupo,emails,schema,warehouse,modelos,tpm,qpm\nFin-X,nao-email,,,,abc,\nok,a@x.com,s,,,,\nok,a@x.com,s,,,,\n"
        _, erros = lib.parse_csv(txt, D)
        texto = " | ".join(erros)
        for esperado in ["nome de grupo", "e-mail inválido", "schema obrigatório", "tpm inválido", "grupo repetido"]:
            self.assertIn(esperado, texto)

    def test_colunas_ausentes_e_bom(self):
        _, erros = lib.parse_csv("grupo,emails\nx,a@x.com\n", D)
        self.assertIn("colunas ausentes", erros[0])
        grupos, erros = lib.parse_csv("﻿grupo,emails,schema,warehouse,modelos,tpm,qpm\nx,,s,,,,\n", D)
        self.assertEqual((erros, grupos[0]["grupo"]), ([], "x"))

    def test_validate_models(self):
        erros = lib.validate_models([{"grupo": "g", "modelos": ["a", "b"]}], {"a": {}})
        self.assertEqual(len(erros), 1)
        self.assertIn("'b'", erros[0])


class TestRateLimits(unittest.TestCase):
    def test_zero_preserva_outros_limites(self):
        outro = {"key": "RATE_LIMIT_KEY_SERVICE", "tokens": 5}
        novo = lib.zero_limits_for([outro, {"key": "RATE_LIMIT_KEY_USER_DEFAULT", "tokens": 10}])
        self.assertIn(outro, novo)
        self.assertEqual([r for r in novo if r["key"] == "RATE_LIMIT_KEY_USER_DEFAULT"], lib.ZERO_LIMITS)

    def test_zero_idempotente(self):
        self.assertIsNone(lib.zero_limits_for(lib.ZERO_LIMITS))
        como_texto = [dict(r, **{k: "0" for k in ("tokens", "requests") if k in r}) for r in lib.ZERO_LIMITS]
        self.assertIsNone(lib.zero_limits_for(como_texto))

    def test_tpm_qpm_vazio_ou_zero_sem_limite(self):
        txt = "grupo,emails,schema,warehouse,modelos,tpm,qpm\na,,s,,,0,\nb,,t,,,500,0\n"
        g, e = lib.parse_csv(txt, D)
        self.assertEqual((e, [(x["tpm"], x["qpm"]) for x in g]), ([], [(None, None), (500, None)]))
        self.assertEqual(lib.group_rate_limits("a", None, None), [])
        self.assertEqual(len(lib.group_rate_limits("b", 500, None)), 1)
        self.assertEqual(lib.describe_limits(None, None), "TPM sem limite / QPM sem limite")

    def test_group_limits(self):
        lim = lib.group_rate_limits("g", 100, 5)
        self.assertEqual({(r["principal"], r.get("tokens"), r.get("requests")) for r in lim}, {("g", 100, None), ("g", None, 5)})

    def test_service_name(self):
        self.assertEqual(lib.service_name("gpt-5-mini"), "genai_gpt_5_mini")


class TestEtapas(unittest.TestCase):
    def test_simular_nao_escreve(self):
        api = FakeApi()
        lib.ensure_group(api, "g", False)
        lib.ensure_user(api, "a@x.com", False)
        lib.ensure_member(api, None, None, False)
        lib.ensure_workspace_assignment(api, None, ENT, False)
        lib.ensure_schema(api, "cat", "s", "g", "adm", PRIV, ["USE_CATALOG"], False)
        lib.ensure_warehouse(api, "wh", "g", "budget", "fin", WH, False)
        lib.ensure_model_service(api, "cat", "s", "g", "m", "models/x", 1, 1, False)
        self.assertEqual(api.writes(), [])

    def test_aplicar_cria_tudo(self):
        api = FakeApi()
        self.assertEqual(lib.ensure_group(api, "g", True), ("criado", "101"))
        self.assertEqual(lib.ensure_user(api, "ana.silva@x.com", True), ("criado", "202"))
        body = [c[2] for c in api.calls if c[1] == "/api/2.0/identity/users"][0]
        self.assertEqual(body["full_name"], {"given_name": "Ana", "family_name": "Silva"})
        self.assertEqual(lib.ensure_workspace_assignment(api, "101", ENT, True)[0], "criado")
        self.assertEqual(lib.ensure_schema(api, "cat", "s", "g", "adm", PRIV, ["USE_CATALOG"], True)[0], "criado")
        grants = [c for c in api.calls if c[0] == "PATCH" and "/permissions/schema/" in c[1]][0][2]
        self.assertEqual(grants["changes"][0]["add"], PRIV)
        self.assertNotIn("CREATE_VOLUME", grants["changes"][0]["add"])
        self.assertEqual(lib.ensure_warehouse(api, "wh", "g", "budget", "fin", WH, True)[0], "criado")
        self.assertEqual(lib.ensure_model_service(api, "cat", "s", "g", "gpt-5-mini", "models/x", 100, 5, True)[0], "criado")
        self.assertTrue(any("/permissions/model_service/cat.s.genai_gpt_5_mini" in c[1] for c in api.calls))

    def test_reexecucao_reaproveita(self):
        api = FakeApi(groups={"g": "1"}, users={"a@x.com": "2"}, assigned={"1"}, schemas={"cat.s"},
                      warehouses=[{"name": "wh", "id": "w1", "tags": {"custom_tags": [{"key": "budget", "value": "fin"}]}}],
                      services={"cat.s.genai_m"})
        self.assertEqual(lib.ensure_group(api, "g", True), ("existe", "1"))
        self.assertEqual(lib.ensure_user(api, "a@x.com", True), ("existe", "2"))
        self.assertEqual(lib.ensure_workspace_assignment(api, "1", ENT, True)[0], "existe")
        self.assertEqual(lib.ensure_schema(api, "cat", "s", "g", "", PRIV, ["USE_CATALOG"], True)[0], "existe")
        self.assertEqual(lib.ensure_warehouse(api, "wh", "g", "budget", "fin", WH, True)[0], "existe")
        self.assertEqual(lib.ensure_model_service(api, "cat", "s", "g", "m", "models/x", 1, 1, True)[0], "atualizado")
        posts = [c for c in api.writes() if c[0] == "POST"]
        self.assertEqual(posts, [])

    def test_membro_ja_existe(self):
        api = FakeApi(members={("1", "2")})
        self.assertEqual(lib.ensure_member(api, "1", "2", True), ("existe", None))
        self.assertEqual(api.writes(), [])

    def test_membro_novo(self):
        api = FakeApi(members={("1", "9")})
        self.assertEqual(lib.ensure_member(api, "1", "2", True), ("criado", None))
        self.assertEqual(lib.ensure_member(api, "1", "2", False)[0], "criaria")


class TestPolicy(unittest.TestCase):
    def _ev(self, action, t, res=None, rp=None):
        import json
        return {"action_name": action, "event_time": t, "result": json.dumps(res) if res else None,
                "request_params": json.dumps(rp or {})}

    def test_policies_nome_id_e_apagadas(self):
        rows = [
            self._ev("createBudgetPolicy", "2026-10-01", {"policy_id": "p1", "policy_name": "exp_fin"}),
            self._ev("createBudgetPolicy", "2026-10-02", {"policy_id": "p2", "policy_name": "exp_risco"}),
            self._ev("deleteBudgetPolicy", "2026-10-03", rp={"policy_id": "p2"}),
            self._ev("updateBudgetPolicy", "2026-10-04", {"policy_id": "p1", "policy_name": "exp_fin_v2"}),
        ]
        self.assertEqual(lib.policies_from_audit(rows), {"exp_fin_v2": "p1"})

    def _rs_api(self, grant_rules):
        calls = []
        def api(m, p, b=None):
            calls.append((m, p, b))
            return {"etag": "e1", "grant_rules": grant_rules} if m == "GET" else {}
        return api, calls

    def test_policy_user_acrescenta_sem_perder_regras(self):
        outra = {"role": "roles/budgetPolicy.manager", "principals": ["users/admin@x.com"]}
        api, calls = self._rs_api([outra])
        self.assertEqual(lib.ensure_policy_user(api, "acc", "p1", "fin", True)[0], "criado")
        body = [c for c in calls if c[0] == "PUT"][0][2]
        self.assertEqual(body["rule_set"]["etag"], "e1")
        self.assertIn(outra, body["rule_set"]["grant_rules"])
        self.assertIn({"role": lib.POLICY_USER_ROLE, "principals": ["groups/fin"]}, body["rule_set"]["grant_rules"])

    def test_policy_user_ja_existe_e_simular(self):
        api, calls = self._rs_api([{"role": lib.POLICY_USER_ROLE, "principals": ["groups/fin"]}])
        self.assertEqual(lib.ensure_policy_user(api, "acc", "p1", "fin", True)[0], "existe")
        api, calls = self._rs_api([])
        self.assertEqual(lib.ensure_policy_user(api, "acc", "p1", "fin", False)[0], "criaria")
        self.assertEqual([c for c in calls if c[0] == "PUT"], [])

    def test_csv_coluna_opcional(self):
        base = "grupo,emails,schema,warehouse,modelos,tpm,qpm"
        g, e = lib.parse_csv(base + "\nx,,s,,,,\n", D)
        self.assertEqual((e, g[0]["usage_policy"]), ([], ""))
        g, e = lib.parse_csv(base + ",usage_policy\nx,,s,,,,, exp_fin \n", D)
        self.assertEqual(g[0]["usage_policy"], "exp_fin")


class TestModelServiceLimits(unittest.TestCase):
    def test_sem_mudanca_nao_regrava(self):
        api = FakeApi(services={"cat.s.genai_m"})
        api.service_limits = [dict(r, **{k: str(v) for k, v in r.items() if k in ("tokens", "requests")})
                              for r in lib.group_rate_limits("g", 100, 5)]
        self.assertEqual(lib.ensure_model_service(api, "cat", "s", "g", "m", "models/x", 100, 5, True)[0], "existe")
        self.assertFalse(any("update_mask" in c[1] for c in api.calls))

    def test_limite_mudou_regrava(self):
        api = FakeApi(services={"cat.s.genai_m"})
        api.service_limits = lib.group_rate_limits("g", 100, 5)
        self.assertEqual(lib.ensure_model_service(api, "cat", "s", "g", "m", "models/x", 200, 5, True)[0], "atualizado")
        self.assertEqual(lib.ensure_model_service(api, "cat", "s", "g", "m", "models/x", 200, 5, False)[0], "atualizaria")


class TestWarehouseTag(unittest.TestCase):
    def test_tag_nova_mantem_outras_e_config(self):
        wh = {"name": "wh", "id": "w1", "cluster_size": "2X-Small", "auto_stop_mins": 10,
              "tags": {"custom_tags": [{"key": "grupo", "value": "x"}, {"key": "budget", "value": "antigo"}]}}
        api = FakeApi(warehouses=[wh])
        self.assertEqual(lib.ensure_warehouse(api, "wh", "g", "budget", "fin", WH, False)[0], "atualizaria")
        self.assertEqual(lib.ensure_warehouse(api, "wh", "g", "budget", "fin", WH, True)[0], "atualizado")
        body = [c for c in api.calls if c[1].endswith("/edit")][0][2]
        self.assertEqual(body["cluster_size"], "2X-Small")
        self.assertEqual(body["tags"]["custom_tags"], [{"key": "grupo", "value": "x"}, {"key": "budget", "value": "fin"}])

    def test_csv_budget_padrao_e_explicito(self):
        base = "grupo,emails,schema,warehouse,modelos,tpm,qpm,budget"
        g, _ = lib.parse_csv(base + "\nfin_users,,s,,,,,\nrisco_users,,r,,,,,riscos\n", D)
        self.assertEqual([x["budget"] for x in g], ["fin_users", "riscos"])


class TestSetting(unittest.TestCase):
    def _api(self, valor):
        calls = []
        def api(m, p, b=None):
            calls.append((m, p, b))
            return {"effective_boolean_val": {"value": valor}} if m == "GET" else {}
        return api, calls

    def test_setting(self):
        api, calls = self._api(True)
        self.assertEqual(lib.ensure_bool_setting(api, "x", True, True)[0], "existe")
        api, calls = self._api(False)
        self.assertEqual(lib.ensure_bool_setting(api, "x", True, False)[0], "atualizaria")
        self.assertEqual(lib.ensure_bool_setting(api, "x", True, True)[0], "atualizado")
        self.assertEqual(calls[-1][2], {"name": "x", "boolean_val": {"value": True}})


class TestWarehouseServerless(unittest.TestCase):
    def test_cria_sempre_serverless_pro(self):
        api = FakeApi()
        lib.ensure_warehouse(api, "wh", "g", "budget", "fin", WH, True)
        body = [c[2] for c in api.calls if c[0] == "POST" and c[1] == "/api/2.0/sql/warehouses"][0]
        self.assertEqual((body["enable_serverless_compute"], body["warehouse_type"]), (True, "PRO"))
        self.assertEqual((body["max_num_clusters"], body["auto_stop_mins"]), (3, 5))


class TestAssignment(unittest.TestCase):
    def test_ja_atribuido_na_criacao_conta_como_existe(self):
        def api(m, p, b=None):
            if m == "GET":
                raise lib.ApiError("not directly assigned", status=404)
            raise lib.ApiError("Principal 1 is already assigned to workspace 2.")
        self.assertEqual(lib.ensure_workspace_assignment(api, "1", ENT, True)[0], "existe")


class TestSystemAi(unittest.TestCase):
    def _api(self):
        calls = []
        def api(m, p, b=None):
            calls.append((m, p, b))
            return {}
        return api, calls

    def test_zerar_simular_aplicar_e_idempotente(self):
        servicos = {"a": {"rate_limits": []}, "b": {"rate_limits": lib.ZERO_LIMITS}}
        api, calls = self._api()
        self.assertEqual([r[1] for r in lib.apply_system_ai(api, servicos, "zerar", False)], ["atualizaria", "existe"])
        self.assertEqual(calls, [])
        self.assertEqual([r[1] for r in lib.apply_system_ai(api, servicos, "zerar", True)], ["atualizado", "existe"])
        self.assertEqual(len(calls), 1)

    def test_remover_zero_preserva_outros(self):
        outro = {"key": "RATE_LIMIT_KEY_SERVICE", "tokens": 5}
        servicos = {"a": {"rate_limits": lib.ZERO_LIMITS + [outro]}, "b": {"rate_limits": []}}
        api, calls = self._api()
        self.assertEqual([r[1] for r in lib.apply_system_ai(api, servicos, "remover_zero", True)], ["atualizado", "existe"])
        self.assertEqual(calls[0][2]["config"]["rate_limits"], [outro])

    def test_manage(self):
        def api(m, p, b=None):
            return {"privilege_assignments": [{"principal": "eu", "privileges": ["MANAGE"]}]} if m == "GET" else {}
        self.assertEqual(lib.grant_manage_system_ai(api, "eu", True)[0], "existe")
        self.assertEqual(lib.grant_manage_system_ai(api, "outro", False)[0], "criaria")


class TestTag(unittest.TestCase):
    def _api(self, tags):
        calls = []
        def api(m, p, b=None):
            calls.append((m, p, b))
            return {"tag_assignments": tags} if m == "GET" else {}
        return api, calls

    def test_tag_cria_existe_atualiza(self):
        api, calls = self._api([])
        self.assertEqual(lib.ensure_tag(api, "model-services", "c.s.m", "grupo", "fin", True)[0], "criado")
        api, calls = self._api([{"tag_key": "grupo", "tag_value": "fin"}])
        self.assertEqual(lib.ensure_tag(api, "model-services", "c.s.m", "grupo", "fin", True)[0], "existe")
        api, calls = self._api([{"tag_key": "grupo", "tag_value": "outro"}])
        self.assertEqual(lib.ensure_tag(api, "model-services", "c.s.m", "grupo", "fin", True)[0], "atualizado")
        self.assertTrue(any(c[0] == "PATCH" for c in calls))


if __name__ == "__main__":
    unittest.main()
