# Databricks notebook source
# MAGIC %md
# MAGIC # Monitoramento de custos — setup (ambiente de experimentação)
# MAGIC
# MAGIC Cria a base do monitoramento, o **dashboard com todas as áreas** (admins) e **um dashboard por área**, em que o
# MAGIC grupo da área vê só os custos da própria tag. Não depende do `onboarding_grupos` e não altera nada dele.
# MAGIC Rode de novo sempre que incluir, remover ou mudar uma área (campo 6) — nada é duplicado.
# MAGIC
# MAGIC **Primeiro rode a célula "Parâmetros"**: os campos aparecem no topo. Preencha-os e rode a célula 0 e as etapas
# MAGIC em ordem (ou **Run all**).
# MAGIC
# MAGIC | Etapa | O que faz |
# MAGIC |---|---|
# MAGIC | 1. Schema e áreas | schema de monitoramento e a tabela `cad_areas` (as áreas do campo 6) |
# MAGIC | 2. Materialized views | `mv_billing` (consumo + preço de tabela) e `mv_genai_uso` (chamadas e tokens): system tables filtradas **por este workspace**, com refresh agendado |
# MAGIC | 3. Views e acessos | `vw_custo`, `vw_genai`, `vw_areas` (MV + área pela tag, com **filtro por linha**); `SELECT` nas views para o grupo de cada área |
# MAGIC | 4. Dashboards | o geral (todas as áreas) e o de cada área (no warehouse da área, `CAN_RUN` só para o grupo dela) |
# MAGIC | 5. Resumo | tabela com todas as etapas executadas |
# MAGIC
# MAGIC **Campo 6 — áreas:** `tag=grupo=warehouse`, separados por vírgula. Ex.: `financas=fin_users=wh_financas, riscos=risco_users=wh_riscos`.
# MAGIC - `tag`: valor da tag de custo (`budget`) dos recursos da área. É o que define a área de cada custo: valores que não
# MAGIC   são de nenhuma área aparecem como "tag sem área"; consumo sem a tag, como "sem tag" (os dois só para os admins).
# MAGIC - `grupo`: quem vê essa área (grupo de account já existente).
# MAGIC - `warehouse`: onde rodam as consultas do dashboard da área (o grupo precisa ter acesso a ele).
# MAGIC
# MAGIC **Arquivos na mesma pasta:** este notebook, `monitoramento_lib.py` e `dashboard_custos.lvdash.json`.
# MAGIC
# MAGIC Quem executa: workspace admin, com `SELECT` em `system.billing`, `system.ai_gateway` e `system.query`, e
# MAGIC `USE_CATALOG`/`CREATE_SCHEMA` no catálogo. As MVs e as views rodam com a identidade de quem as criou (o owner):
# MAGIC os grupos só recebem acesso às views (etapa 3), nunca às system tables nem às MVs.

# COMMAND ----------

# DBTITLE 1,Parâmetros (rode primeiro: os campos aparecem no topo do notebook)
dbutils.widgets.dropdown("modo", "simular", ["simular", "aplicar"], "1. Modo")
dbutils.widgets.text("catalogo", "", "2. Catálogo (já existente)")
dbutils.widgets.text("schema", "monitoramento", "3. Schema de monitoramento")
dbutils.widgets.text("warehouse", "", "4. Warehouse dos admins (nome; executa o setup e o dashboard dos admins)")
dbutils.widgets.text("grupo_admin", "", "5. Grupo admin (vê todas as áreas; opcional)")
dbutils.widgets.text("areas", "", "6. Áreas: tag=grupo=warehouse, separados por vírgula")

_faltando = [r for c, r in [("catalogo", "2. Catálogo"), ("warehouse", "4. Warehouse dos admins"),
                            ("areas", "6. Áreas")]
             if not dbutils.widgets.get(c).strip()]
print("Campos prontos no topo do notebook. Preencha-os e rode as próximas células (ou Run all).")
print("⚠️ Obrigatório e ainda vazio: " + ", ".join(_faltando) if _faltando else "✔ Campos obrigatórios preenchidos.")

# COMMAND ----------

# DBTITLE 1,0. Configuração e validação
# ---------------------------------------------------------------- configuração do monitoramento ----
CONFIG = {
    # Chave da tag de custo que define a área (a mesma usada no onboarding_grupos)
    "tag_custo": "budget",
    # Refresh das materialized views (Quartz cron). O billing não é em tempo real; cada refresh recalcula a MV inteira.
    "refresh_cron": "0 0/30 * * * ?",
    "fuso": "America/Sao_Paulo",
    # Pasta dos dashboards (dentro da pasta pessoal do admin; cada dashboard recebe a própria permissão)
    "pasta_dashboards": "/Workspace/Users/{admin}/experimentacao_dashboards",
    "titulo_admins": "Custos do ambiente de experimentação — todas as áreas",
    "titulo_area": "Custos — {area}",
}

APLICAR = dbutils.widgets.get("modo") == "aplicar"
CATALOGO = dbutils.widgets.get("catalogo").strip()
SCHEMA = dbutils.widgets.get("schema").strip() or "monitoramento"
WAREHOUSE = dbutils.widgets.get("warehouse").strip()
GRUPO_ADMIN = dbutils.widgets.get("grupo_admin").strip()

import importlib, json, os, sys, time
from urllib.parse import urlsplit, parse_qsl

_nb = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
NB_DIR = "/Workspace" + os.path.dirname(_nb)
sys.path.insert(0, NB_DIR)
import monitoramento_lib as mon
importlib.reload(mon)
AREAS, _erros_areas = mon.parse_areas(dbutils.widgets.get("areas"))

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import DatabricksError, NotFound

w = WorkspaceClient()


def do(method, path, body=None):
    parts = urlsplit(path)
    try:
        return w.api_client.do(method, parts.path, query=dict(parse_qsl(parts.query, keep_blank_values=True)) or None,
                               body=body) or {}
    except DatabricksError as e:
        raise mon.ApiError(str(e), status=404 if isinstance(e, NotFound) else None,
                           error_code=getattr(e, "error_code", "") or "") from e


def sql(statement):
    """Executa no warehouse dos admins (o mesmo motor em que as MVs e as views foram validadas)."""
    r = w.statement_execution.execute_statement(statement=statement, warehouse_id=WAREHOUSE_ID, wait_timeout="50s")
    while r.status.state.value in ("PENDING", "RUNNING"):
        time.sleep(5)
        r = w.statement_execution.get_statement(r.statement_id)
    if r.status.state.value != "SUCCEEDED":
        raise Exception(f"SQL falhou: {r.status.error.message if r.status.error else r.status.state.value}")
    return r


RESULTADO = []


def registrar(etapa, item, fn, *args):
    try:
        st, det = fn(*args)
    except Exception as e:
        RESULTADO.append((etapa, item, "ERRO", str(e)[:500]))
        return "ERRO", None
    RESULTADO.append((etapa, item, st, "" if det is None else str(det)))
    return st, det


def nova_etapa(etapa):
    RESULTADO[:] = [r for r in RESULTADO if r[0] != etapa]
    return etapa


def mostrar(etapa):
    linhas = [r for r in RESULTADO if r[0] == etapa]
    if linhas:
        display(spark.createDataFrame(linhas, "etapa string, item string, status string, detalhe string"))
    n_err = sum(1 for r in linhas if r[2] == "ERRO")
    print(f"{'❌' if n_err else '✔'} {etapa}: {len(linhas)} item(ns), {n_err} erro(s)")


# --- validação ---
erros = []
_amplos = mon.broad_access_on_folder(do, NB_DIR)
if _amplos:
    erros.append(f"a pasta {NB_DIR} está acessível a {', '.join(_amplos)}: importe na pasta pessoal do admin")
for campo, valor in [("2. Catálogo", CATALOGO), ("3. Schema", SCHEMA)]:
    try:
        mon.valida_identificador(valor, campo)
    except ValueError as e:
        erros.append(str(e))
if CATALOGO and not erros:
    try:
        do("GET", f"/api/2.1/unity-catalog/catalogs/{CATALOGO}")
    except mon.ApiError as e:
        erros.append(f"catálogo '{CATALOGO}' não encontrado ou sem acesso: {e}")
WAREHOUSE_ID = mon.warehouse_id_by_name(do, WAREHOUSE) if WAREHOUSE else None
if not WAREHOUSE_ID:
    erros.append(f"warehouse '{WAREHOUSE}' não encontrado (campo '4. Warehouse dos admins')")
erros += [f"campo '6. Áreas': {e}" for e in _erros_areas]
for a in AREAS:
    if not mon.group_exists(do, a["grupo"]):
        erros.append(f"área '{a['tag']}': grupo '{a['grupo']}' não existe")
    a["warehouse_id"] = mon.warehouse_id_by_name(do, a["warehouse"])
    if not a["warehouse_id"]:
        erros.append(f"área '{a['tag']}': warehouse '{a['warehouse']}' não encontrado")
if GRUPO_ADMIN and not mon.group_exists(do, GRUPO_ADMIN):
    erros.append(f"grupo admin '{GRUPO_ADMIN}' não existe")
try:
    TEMPLATE = open(os.path.join(NB_DIR, "dashboard_custos.lvdash.json"), encoding="utf-8").read()
except Exception as e:
    erros.append(f"modelo do dashboard não encontrado em {NB_DIR}: {e}")

EXECUTOR = w.current_user.me().user_name
WORKSPACE_ID = str(w.get_workspace_id())
SCHEMA_FULL = f"{CATALOGO}.{SCHEMA}"
PASTA_DASH = CONFIG["pasta_dashboards"].format(admin=EXECUTOR)

if erros:
    raise Exception("Validação falhou — corrija antes de continuar (nada foi alterado):\n- " + "\n- ".join(erros))
print(f"✔ modo = {'APLICAR' if APLICAR else 'SIMULAR (nada é alterado)'} · workspace {WORKSPACE_ID} · "
      f"schema {SCHEMA_FULL} · warehouse {WAREHOUSE} ({WAREHOUSE_ID}) · refresh {CONFIG['refresh_cron']} {CONFIG['fuso']}")
print(f"  dashboards em {PASTA_DASH} · grupo admin: {GRUPO_ADMIN or '— (só os admins do workspace)'}")
for a in AREAS:
    print(f"  - área {a['tag']} (tag {CONFIG['tag_custo']}={a['tag']}): vê o grupo {a['grupo']}, dashboard no warehouse {a['warehouse']}")

# COMMAND ----------

# DBTITLE 1,1. Schema e áreas
ETAPA = nova_etapa("1. schema e áreas")


def _schema(aplicar):
    try:
        do("GET", f"/api/2.1/unity-catalog/schemas/{SCHEMA_FULL}")
        return "existe", SCHEMA_FULL
    except mon.ApiError as e:
        if not mon._not_found(e):
            raise
    if not aplicar:
        return "criaria", SCHEMA_FULL
    do("POST", "/api/2.1/unity-catalog/schemas",
       {"name": SCHEMA, "catalog_name": CATALOGO, "comment": "Monitoramento de custos do ambiente de experimentação"})
    return "criado", SCHEMA_FULL


registrar(ETAPA, SCHEMA_FULL, _schema, APLICAR)
_query = lambda q: [r.asDict() for r in spark.sql(q).collect()]
registrar(ETAPA, "cad_areas", mon.ensure_areas, _query, sql, SCHEMA_FULL, AREAS, EXECUTOR,
          bool(mon.get_table(do, f"{SCHEMA_FULL}.cad_areas")), APLICAR)
mostrar(ETAPA)

# COMMAND ----------

# DBTITLE 1,2. Materialized views (filtradas por este workspace)
ETAPA = nova_etapa("2. materialized views")
for nome, select in mon.mv_definitions(WORKSPACE_ID).items():
    registrar(ETAPA, nome, mon.ensure_mv, do, sql, f"{SCHEMA_FULL}.{nome}", select,
              CONFIG["refresh_cron"], CONFIG["fuso"], APLICAR)
mostrar(ETAPA)

# COMMAND ----------

# DBTITLE 1,3. Views e acessos das áreas
ETAPA = nova_etapa("3. views e acessos")
for nome, select in mon.view_definitions(SCHEMA_FULL, CONFIG["tag_custo"], GRUPO_ADMIN).items():
    registrar(ETAPA, nome, mon.ensure_view, do, sql, f"{SCHEMA_FULL}.{nome}", select, APLICAR)
# Sempre confere os acessos: recriar uma view apaga as permissões dela.
_grupos = list(dict.fromkeys(([GRUPO_ADMIN] if GRUPO_ADMIN else []) + [a["grupo"] for a in AREAS]))
for grupo in _grupos:
    registrar(ETAPA, f"{grupo}: schema", mon.ensure_grant, do, "schema", SCHEMA_FULL, grupo, ["USE_SCHEMA"], APLICAR)
    for nome in mon.VIEWS:
        registrar(ETAPA, f"{grupo}: {nome}", mon.ensure_grant, do, "table", f"{SCHEMA_FULL}.{nome}", grupo,
                  ["SELECT"], APLICAR)
mostrar(ETAPA)

# COMMAND ----------

# DBTITLE 1,4. Dashboards (geral e por área)
ETAPA = nova_etapa("4. dashboards")
registrar(ETAPA, PASTA_DASH, mon.ensure_folder, do, PASTA_DASH, APLICAR)
_links = []
_paineis = [(CONFIG["titulo_admins"], WAREHOUSE_ID, GRUPO_ADMIN or None)]
for a in AREAS:
    if not mon.group_can_use_warehouse(do, a["warehouse_id"], a["grupo"]):
        RESULTADO.append((ETAPA, a["warehouse"], "ALERTA",
                          f"o grupo {a['grupo']} não tem CAN_USE no warehouse {a['warehouse']}: o dashboard da área "
                          "não vai carregar para ele (o acesso ao warehouse é dado pelo onboarding_grupos)"))
    _paineis.append((CONFIG["titulo_area"].format(area=a["tag"]), a["warehouse_id"], a["grupo"]))
for titulo, wid, grupo in _paineis:
    registrar(ETAPA, titulo, mon.ensure_dashboard, do, PASTA_DASH, titulo,
              mon.render_dashboard(TEMPLATE, SCHEMA_FULL, titulo), wid, grupo, APLICAR)
    _id = mon.find_dashboard_id(do, PASTA_DASH, titulo)
    if _id:
        _links.append((titulo, f"{w.config.host.rstrip('/')}/dashboardsv3/{_id}/published?o={WORKSPACE_ID}"))
mostrar(ETAPA)
for titulo, link in _links:
    print(f"{titulo}: {link}")

# COMMAND ----------

# DBTITLE 1,5. Resumo
if RESULTADO:
    display(spark.createDataFrame(RESULTADO, "etapa string, item string, status string, detalhe string"))
n_err = sum(1 for r in RESULTADO if r[2] == "ERRO")
n_alerta = sum(1 for r in RESULTADO if r[2] == "ALERTA")
print(f"{'❌' if n_err else '⚠️' if n_alerta else '✔'} {len(RESULTADO)} item(ns) · {n_err} erro(s) · {n_alerta} alerta(s) · "
      f"modo {'aplicar' if APLICAR else 'simular'}")
dbutils.notebook.exit(json.dumps({"modo": "aplicar" if APLICAR else "simular", "erros": n_err, "alertas": n_alerta,
                                  "itens": [dict(zip(["etapa", "item", "status", "detalhe"], r)) for r in RESULTADO]}))
