import json, os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "monitoramento"))
import monitoramento_lib as mon

RAIZ = os.path.join(os.path.dirname(__file__), "..")
TEMPLATE = os.path.join(RAIZ, "monitoramento", "dashboard_custos.lvdash.json")


class FakeApi:
    def __init__(self, tables=None, dashboards=None, published=None, perms=None):
        self.tables = dict(tables or {})          # full_name -> dict
        self.dashboards = dict(dashboards or {})  # path -> {"id":..., "serialized_dashboard":..., "warehouse_id":...}
        self.published = dict(published or {})    # id -> dict
        self.perms = dict(perms or {})            # (securable, full) -> {principal: set}
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        base = path.split("?")[0]
        if method == "GET" and base.startswith("/api/2.1/unity-catalog/tables/"):
            t = self.tables.get(base.rsplit("/", 1)[1])
            if t is None:
                raise mon.ApiError("x", status=404)
            return t
        if method == "GET" and base == "/api/2.0/workspace/get-status":
            from urllib.parse import unquote
            p = unquote(path.split("path=")[1])
            d = self.dashboards.get(p)
            if d is None:
                raise mon.ApiError("x", status=404)
            return {"resource_id": d["id"]}
        if method == "GET" and base.startswith("/api/2.0/lakeview/dashboards/") and base.endswith("/published"):
            did = base.split("/")[-2]
            if did not in self.published:
                raise mon.ApiError("x", status=404)
            return self.published[did]
        if method == "GET" and base.startswith("/api/2.0/lakeview/dashboards/"):
            did = base.rsplit("/", 1)[1]
            return next(d for d in self.dashboards.values() if d["id"] == did)
        if method == "POST" and base == "/api/2.0/lakeview/dashboards":
            return {"dashboard_id": "d-new"}
        if method == "GET" and base.startswith("/api/2.1/unity-catalog/permissions/"):
            sec, full = base.split("/")[5], base.split("/", 6)[6]
            atual = self.perms.get((sec, full), {})
            return {"privilege_assignments": [{"principal": p, "privileges": sorted(v)} for p, v in atual.items()]}
        return {}

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


class TestMvs(unittest.TestCase):
    def test_filtro_do_workspace_em_todas(self):
        for nome, sel in mon.mv_definitions("123").items():
            self.assertIn("workspace_id = '123'", sel, nome)
        self.assertEqual(set(mon.mv_definitions("1")), set(mon.MVS))

    def test_create_tem_agendamento_e_comentario(self):
        q = mon.create_mv_sql("c.s.mv_x", "SELECT 1", "0 0/30 * * * ?", "America/Sao_Paulo")
        self.assertIn("SCHEDULE CRON '0 0/30 * * * ?' AT TIME ZONE 'America/Sao_Paulo'", q)
        self.assertIn(mon.mv_comment("0 0/30 * * * ?", "America/Sao_Paulo"), q)

    def test_mv_igual_nao_recria(self):
        sel = "SELECT 1\n  FROM x"
        api = FakeApi(tables={"c.s.mv": {"view_definition": " SELECT 1 FROM x ", "comment": mon.mv_comment("C", "F")}})
        executados = []
        self.assertEqual(mon.ensure_mv(api, executados.append, "c.s.mv", sel, "C", "F", True)[0], "existe")
        self.assertEqual(executados, [])

    def test_mv_agendamento_mudou_recria(self):
        api = FakeApi(tables={"c.s.mv": {"view_definition": "SELECT 1", "comment": mon.mv_comment("A", "F")}})
        executados = []
        self.assertEqual(mon.ensure_mv(api, executados.append, "c.s.mv", "SELECT 1", "B", "F", False)[0], "atualizaria")
        self.assertEqual(executados, [])
        self.assertEqual(mon.ensure_mv(api, executados.append, "c.s.mv", "SELECT 1", "B", "F", True)[0], "atualizado")
        self.assertEqual(len(executados), 1)

    def test_mv_nova(self):
        executados = []
        self.assertEqual(mon.ensure_mv(FakeApi(), executados.append, "c.s.mv", "SELECT 1", "C", "F", True)[0], "criado")


class TestViews(unittest.TestCase):
    def test_filtro_por_linha_em_todas(self):
        for nome, sel in mon.view_definitions("c.m", "budget", "").items():
            self.assertIn("is_account_group_member(", sel, nome)
            self.assertIn("is_member('admins')", sel, nome)

    def test_area_vem_da_tag_via_cad_areas(self):
        v = mon.view_definitions("c.m", "budget", "")
        self.assertIn("LEFT JOIN c.m.cad_areas a ON a.budget = b.custom_tags['budget']", v["vw_custo"])
        self.assertIn("LEFT JOIN c.m.cad_areas a ON a.budget = g.service_tags['budget']", v["vw_genai"])
        for nome in ("vw_custo", "vw_genai"):
            self.assertNotIn("WITH", v[nome])     # views simples: uma junção, sem CTEs
            self.assertIn("is_account_group_member(a.grupo)", v[nome])

    def test_grupo_admin_opcional(self):
        self.assertEqual(mon.admin_expr(""), "is_member('admins')")
        self.assertIn("is_account_group_member('exp_admins')", mon.admin_expr("exp_admins"))

    def test_chave_da_tag_configuravel(self):
        self.assertIn("custom_tags['centro_custo']", mon.view_definitions("c.m", "centro_custo", "")["vw_custo"])

    def test_view_igual_nao_recria(self):
        api = FakeApi(tables={"c.m.vw": {"view_definition": "SELECT  1"}})
        executados = []
        self.assertEqual(mon.ensure_view(api, executados.append, "c.m.vw", "SELECT 1", True)[0], "existe")
        self.assertEqual(mon.ensure_view(api, executados.append, "c.m.vw", "SELECT 2", True)[0], "atualizado")
        self.assertEqual(len(executados), 1)

    def test_literal_escapa_aspas(self):
        self.assertEqual(mon._lit("a'b"), "'a\\'b'")


class TestGrants(unittest.TestCase):
    def test_so_concede_o_que_falta(self):
        api = FakeApi(perms={("table", "c.m.vw"): {"g": {"SELECT"}}})
        self.assertEqual(mon.ensure_grant(api, "table", "c.m.vw", "g", ["SELECT"], True)[0], "existe")
        self.assertEqual(api.writes(), [])
        self.assertEqual(mon.ensure_grant(api, "schema", "c.m", "g", ["USE_SCHEMA"], False)[0], "criaria")
        self.assertEqual(api.writes(), [])
        self.assertEqual(mon.ensure_grant(api, "schema", "c.m", "g", ["USE_SCHEMA"], True)[0], "criado")
        self.assertEqual(api.writes()[0][2], {"changes": [{"principal": "g", "add": ["USE_SCHEMA"]}]})


class TestDashboard(unittest.TestCase):
    def test_render_troca_marcadores(self):
        tpl = json.dumps({"q": f"SELECT * FROM {mon.PLACEHOLDER_SCHEMA}.vw", "t": mon.PLACEHOLDER_TITULO})
        out = json.loads(mon.render_dashboard(tpl, "c.m", "Custos — fin"))
        self.assertEqual(out, {"q": "SELECT * FROM c.m.vw", "t": "Custos — fin"})

    def test_cria_publica_sem_credenciais_e_da_can_run(self):
        api = FakeApi()
        st, _ = mon.ensure_dashboard(api, "/p", "Custos — fin", "{}", "wh1", "fin", True)
        self.assertEqual(st, "criado")
        pub = [c for c in api.calls if c[1].endswith("/published") and c[0] == "POST"][0][2]
        self.assertEqual(pub, {"embed_credentials": False, "warehouse_id": "wh1"})
        acl = [c for c in api.calls if c[1].startswith("/api/2.0/permissions/dashboards/")][0][2]
        self.assertEqual(acl, {"access_control_list": [{"group_name": "fin", "permission_level": "CAN_RUN"}]})

    def test_simular_nao_escreve(self):
        api = FakeApi()
        self.assertEqual(mon.ensure_dashboard(api, "/p", "X", "{}", "wh1", "fin", False)[0], "criaria")
        self.assertEqual(api.writes(), [])

    def test_igual_e_publicado_existe(self):
        api = FakeApi(dashboards={"/p/X.lvdash.json": {"id": "d1", "serialized_dashboard": "{}", "warehouse_id": "wh1"}},
                      published={"d1": {"embed_credentials": False, "warehouse_id": "wh1"}})
        self.assertEqual(mon.ensure_dashboard(api, "/p", "X", "{}", "wh1", None, True)[0], "existe")
        self.assertEqual(api.writes(), [])

    def test_publicado_com_credenciais_embutidas_republica(self):
        api = FakeApi(dashboards={"/p/X.lvdash.json": {"id": "d1", "serialized_dashboard": "{}", "warehouse_id": "wh1"}},
                      published={"d1": {"embed_credentials": True, "warehouse_id": "wh1"}})
        self.assertEqual(mon.ensure_dashboard(api, "/p", "X", "{}", "wh1", None, True)[0], "atualizado")
        self.assertFalse(any(c[0] == "PATCH" and "/lakeview/" in c[1] for c in api.calls))   # conteúdo igual
        self.assertTrue(any(c[0] == "POST" and c[1].endswith("/published") for c in api.calls))

    def test_modelo_do_repositorio(self):
        texto = open(TEMPLATE, encoding="utf-8").read()
        self.assertIn(mon.PLACEHOLDER_SCHEMA, texto)
        self.assertIn(mon.PLACEHOLDER_TITULO, texto)
        d = json.loads(mon.render_dashboard(texto, "c.m", "T"))
        datasets = {ds["name"] for ds in d["datasets"]}
        for ds in d["datasets"]:
            sql = "".join(ds["queryLines"])
            self.assertNotRegex(sql, r"(?i)(FROM|JOIN)\s+system\.", ds["name"])   # o dashboard só lê as views
            self.assertTrue(any(f"c.m.{v}" in sql for v in mon.VIEWS), ds["name"])
        for p in d["pages"]:
            self.assertEqual(p.get("layoutVersion"), "GRID_V1")
            linhas = {}
            for item in p["layout"]:
                w = item["widget"]
                for q in w.get("queries", []):
                    self.assertIn(q["query"]["datasetName"], datasets)
                if p["pageType"] == "PAGE_TYPE_CANVAS":
                    pos = item["position"]
                    linhas[pos["y"]] = linhas.get(pos["y"], 0) + pos["width"]
            self.assertTrue(all(v == 12 for v in linhas.values()), (p["name"], linhas))


class TestAreas(unittest.TestCase):
    def test_parse_ok(self):
        areas, erros = mon.parse_areas(" financas=fin_users=wh_financas ,\nriscos = risco_users = Serverless Starter Warehouse")
        self.assertEqual(erros, [])
        self.assertEqual(areas, [{"tag": "financas", "grupo": "fin_users", "warehouse": "wh_financas"},
                                 {"tag": "riscos", "grupo": "risco_users", "warehouse": "Serverless Starter Warehouse"}])

    def test_parse_erros(self):
        _, erros = mon.parse_areas("financas=fin_users, a=b=c, a=d=e")
        texto = " | ".join(erros)
        self.assertIn("use tag=grupo=warehouse", texto)
        self.assertIn("tag 'a' repetida", texto)
        self.assertIn("ao menos uma área", mon.parse_areas("")[1][0])

    AREAS = [{"tag": "financas", "grupo": "fin", "warehouse": "wh", "warehouse_id": "w1"}]

    def test_igual_nao_grava(self):
        executados = []
        q = lambda stmt: [{"budget": "financas", "grupo": "fin", "warehouse": "wh", "warehouse_id": "w1"}]
        self.assertEqual(mon.ensure_areas(q, executados.append, "c.m", self.AREAS, "adm", True, True)[0], "existe")
        self.assertEqual(executados, [])

    def test_mudou_regrava_a_tabela(self):
        executados = []
        q = lambda stmt: [{"budget": "financas", "grupo": "outro", "warehouse": "wh", "warehouse_id": "w1"}]
        self.assertEqual(mon.ensure_areas(q, executados.append, "c.m", self.AREAS, "adm", True, False)[0], "atualizaria")
        self.assertEqual(executados, [])
        self.assertEqual(mon.ensure_areas(q, executados.append, "c.m", self.AREAS, "adm", True, True)[0], "atualizado")
        self.assertIn("CREATE OR REPLACE TABLE c.m.cad_areas", executados[0])
        self.assertIn("('financas', 'fin', 'wh', 'w1')", executados[0])

    def test_tabela_nova(self):
        self.assertEqual(mon.ensure_areas(None, None, "c.m", self.AREAS, "adm", False, False)[0], "criaria")


class TestAcessoWarehouse(unittest.TestCase):
    def _api(self, acl):
        return lambda m, p, b=None: {"access_control_list": acl}

    def test_grupo_com_can_use(self):
        acl = [{"group_name": "fin", "all_permissions": [{"permission_level": "CAN_USE"}]}]
        self.assertTrue(mon.group_can_use_warehouse(self._api(acl), "w1", "fin"))

    def test_sem_acesso(self):
        acl = [{"group_name": "outro", "all_permissions": [{"permission_level": "CAN_USE"}]}]
        self.assertFalse(mon.group_can_use_warehouse(self._api(acl), "w1", "fin"))


class TestDashboardFormatacao(unittest.TestCase):
    def test_json_compacto_do_servidor_conta_como_igual(self):
        modelo = json.dumps({"a": [1, {"b": "x"}]}, indent=2)
        api = FakeApi(dashboards={"/p/X.lvdash.json": {"id": "d1", "serialized_dashboard": '{"a":[1,{"b":"x"}]}',
                                                       "warehouse_id": "wh1"}},
                      published={"d1": {"embed_credentials": False, "warehouse_id": "wh1"}})
        self.assertEqual(mon.ensure_dashboard(api, "/p", "X", modelo, "wh1", None, True)[0], "existe")


class TestGrantGrupoNovo(unittest.TestCase):
    def test_simular_com_grupo_ainda_inexistente(self):
        def api(m, p, b=None):
            if m == "GET":
                raise mon.ApiError("Could not find principal with name g.")
            return {}
        self.assertEqual(mon.ensure_grant(api, "table", "c.m.vw", "g", ["SELECT"], False)[0], "criaria")
