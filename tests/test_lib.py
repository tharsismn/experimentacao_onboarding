import os, sys, unittest
from urllib.parse import unquote
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import onboarding_lib as lib

D = {"warehouse_padrao": "wh_padrao", "tpm_padrao": 1000, "qpm_padrao": 10}


class FakeApi:
    """Simula as APIs: guarda chamadas e responde GETs a partir de um estado em memória."""
    def __init__(self, groups=(), users=(), assigned=(), schemas=(), warehouses=(), services=(), members=()):
        self.members = set(members)
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
                return {}
            raise lib.ApiError("x", error_code="NOT_FOUND")
        if method == "GET" and path == "/api/2.0/sql/warehouses":
            return {"warehouses": self.warehouses}
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
        self.assertEqual((g["warehouse"], g["tpm"], g["qpm"]), ("wh_padrao", 1000, 10))

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
        lib.ensure_workspace_assignment(api, None, False)
        lib.ensure_schema(api, "cat", "s", "g", "adm", False)
        lib.ensure_warehouse(api, "wh", "g", {}, False)
        lib.ensure_model_service(api, "cat", "s", "g", "m", "models/x", 1, 1, False)
        self.assertEqual(api.writes(), [])

    def test_aplicar_cria_tudo(self):
        api = FakeApi()
        self.assertEqual(lib.ensure_group(api, "g", True), ("criado", "101"))
        self.assertEqual(lib.ensure_user(api, "ana.silva@x.com", True), ("criado", "202"))
        body = [c[2] for c in api.calls if c[1] == "/api/2.0/identity/users"][0]
        self.assertEqual(body["full_name"], {"given_name": "Ana", "family_name": "Silva"})
        self.assertEqual(lib.ensure_workspace_assignment(api, "101", True)[0], "criado")
        self.assertEqual(lib.ensure_schema(api, "cat", "s", "g", "adm", True)[0], "criado")
        grants = [c for c in api.calls if c[0] == "PATCH" and "/permissions/schema/" in c[1]][0][2]
        self.assertEqual(grants["changes"][0]["add"], lib.SCHEMA_PRIVILEGES)
        self.assertNotIn("CREATE_VOLUME", grants["changes"][0]["add"])
        self.assertEqual(lib.ensure_warehouse(api, "wh", "g", {"warehouse_tamanho": "2X-Small", "warehouse_max_clusters": 1, "warehouse_auto_stop": 10}, True)[0], "criado")
        self.assertEqual(lib.ensure_model_service(api, "cat", "s", "g", "gpt-5-mini", "models/x", 100, 5, True)[0], "criado")
        self.assertTrue(any("/permissions/model_service/cat.s.genai_gpt_5_mini" in c[1] for c in api.calls))

    def test_reexecucao_reaproveita(self):
        api = FakeApi(groups={"g": "1"}, users={"a@x.com": "2"}, assigned={"1"}, schemas={"cat.s"},
                      warehouses=[{"name": "wh", "id": "w1"}], services={"cat.s.genai_m"})
        self.assertEqual(lib.ensure_group(api, "g", True), ("existe", "1"))
        self.assertEqual(lib.ensure_user(api, "a@x.com", True), ("existe", "2"))
        self.assertEqual(lib.ensure_workspace_assignment(api, "1", True)[0], "existe")
        self.assertEqual(lib.ensure_schema(api, "cat", "s", "g", "", True)[0], "existe")
        self.assertEqual(lib.ensure_warehouse(api, "wh", "g", {}, True)[0], "existe")
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


if __name__ == "__main__":
    unittest.main()
