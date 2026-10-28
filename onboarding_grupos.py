# Databricks notebook source
# MAGIC %md
# MAGIC # Onboarding de grupos — ambiente de experimentação
# MAGIC
# MAGIC Uma célula por etapa. Rode a **célula 0 (parâmetros e validação)** primeiro; depois as etapas
# MAGIC 1 a 5 em ordem (ou **Run all**). Cada etapa pode ser executada de novo sozinha — nada é duplicado.
# MAGIC
# MAGIC | Etapa | O que faz |
# MAGIC |---|---|
# MAGIC | 1. Grupos e usuários | grupo de account, cadastro dos usuários, membros, atribuição ao workspace |
# MAGIC | 2. Schema e permissões | schema no catálogo informado, owner = grupo admin, grants fixos (sem volumes) |
# MAGIC | 3. SQL warehouse | warehouse serverless com tag de custo `budget`, `CAN_USE` p/ o grupo |
# MAGIC | 4. GenAI | model service por modelo, `EXECUTE` só p/ o grupo, tag `budget`, limites TPM/QPM (opcionais) |
# MAGIC | 5. Serverless usage policy | papel **User** do grupo na policy informada (a policy é criada antes, na interface) |
# MAGIC | 6. Resumo | tabela com todas as etapas executadas |
# MAGIC
# MAGIC **CSV** (na mesma pasta deste notebook): `grupo,emails,schema,warehouse,modelos,usage_policy,budget,tpm,qpm` —
# MAGIC `emails` e `modelos` separados por `;`; `warehouse` vazio usa o padrão; `tpm`/`qpm` vazios = **sem limite**;
# MAGIC `usage_policy` = **nome** da serverless usage policy criada em Settings > Compute (vazio = pula a etapa 5);
# MAGIC `budget` = valor da tag de custo `budget` da área (vazio = nome do grupo). Use o **mesmo** valor na tag `budget`
# MAGIC da usage policy, para o custo do serverless, do SQL e do GenAI somar na mesma área.
# MAGIC
# MAGIC Quem executa: workspace admin, com `USE_CATALOG`/`CREATE_SCHEMA` no catálogo e `SELECT` em `system.access.audit`.

# COMMAND ----------

# DBTITLE 1,0. Parâmetros e validação
dbutils.widgets.text("csv_path", "grupos.csv", "1. CSV (nome na pasta do notebook ou caminho completo)")
dbutils.widgets.dropdown("modo", "simular", ["simular", "aplicar"], "2. Modo")
dbutils.widgets.text("catalogo", "", "3. Catálogo (já existente)")
dbutils.widgets.text("grupo_admin", "", "4. Grupo admin (owner dos schemas; opcional)")

# Padrões usados quando a coluna do CSV vem vazia
DEFAULTS = {
    "warehouse_padrao": "wh_experimentacao",
    "warehouse_tamanho": "2X-Small",
    "warehouse_max_clusters": 1,
    "warehouse_auto_stop": 10,   # minutos
}
AUDIT_DIAS = 365                 # janela de busca das policies no audit log

APLICAR = dbutils.widgets.get("modo") == "aplicar"
CATALOGO = dbutils.widgets.get("catalogo").strip()
GRUPO_ADMIN = dbutils.widgets.get("grupo_admin").strip()

import importlib, json, os, sys
from urllib.parse import urlsplit, parse_qsl

_nb = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
NB_DIR = "/Workspace" + os.path.dirname(_nb)
sys.path.insert(0, NB_DIR)
_csv = dbutils.widgets.get("csv_path").strip() or "grupos.csv"
CSV_PATH = _csv if _csv.startswith("/") else os.path.join(NB_DIR, _csv)

import onboarding_lib as lib
importlib.reload(lib)

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import DatabricksError, NotFound, ResourceAlreadyExists, AlreadyExists

w = WorkspaceClient()


def do(method, path, body=None):
    """Adaptador: api_client do SDK → lib.ApiError com status normalizado."""
    parts = urlsplit(path)
    try:
        return w.api_client.do(method, parts.path, query=dict(parse_qsl(parts.query, keep_blank_values=True)) or None, body=body) or {}
    except DatabricksError as e:
        status = 404 if isinstance(e, NotFound) else 409 if isinstance(e, (ResourceAlreadyExists, AlreadyExists)) else None
        raise lib.ApiError(str(e), status=status, error_code=getattr(e, "error_code", "") or "") from e


# Os notebooks rodam com privilégio de admin: a pasta deles não pode ser editável por todos.
_amplos = lib.broad_access_on_folder(do, NB_DIR)
if _amplos:
    raise Exception(f"A pasta {NB_DIR} está acessível a {', '.join(_amplos)}. Publique os notebooks "
                    "na pasta pessoal do admin (/Workspace/Users/<admin>/...) — ver deploy.sh.")

RESULTADO = []   # (etapa, grupo, item, status, detalhe) — acumulado pelas células 1 a 5


def registrar(etapa, grupo, item, fn, *args):
    try:
        st, det = fn(*args)
    except Exception as e:
        RESULTADO.append((etapa, grupo, item, "ERRO", str(e)[:500]))
        return "ERRO", None
    RESULTADO.append((etapa, grupo, item, st, "" if det is None else str(det)))
    return st, det


def nova_etapa(etapa):
    """Ao reexecutar uma célula, descarta o resultado anterior dela."""
    RESULTADO[:] = [r for r in RESULTADO if r[0] != etapa]
    return etapa


def mostrar(etapa):
    linhas = [r for r in RESULTADO if r[0] == etapa]
    if linhas:
        display(spark.createDataFrame(linhas, "etapa string, grupo string, item string, status string, detalhe string"))
    n_err = sum(1 for r in linhas if r[3] == "ERRO")
    n_alerta = sum(1 for r in linhas if r[3] == "ALERTA")
    print(f"{'❌' if n_err else '⚠️' if n_alerta else '✔'} {etapa}: {len(linhas)} item(ns), {n_err} erro(s), {n_alerta} alerta(s)")


# --- validação ---
erros, avisos = [], []
if not CATALOGO:
    erros.append("informe o catálogo")
else:
    try:
        do("GET", f"/api/2.1/unity-catalog/catalogs/{CATALOGO}")
    except lib.ApiError as e:
        erros.append(f"catálogo '{CATALOGO}' não encontrado ou sem acesso: {e}")
if GRUPO_ADMIN and lib.ensure_group(do, GRUPO_ADMIN, aplicar=False)[0] != "existe":
    erros.append(f"grupo admin '{GRUPO_ADMIN}' não existe no account")
try:
    texto = open(CSV_PATH, encoding="utf-8").read()
except Exception as e:
    texto = ""
    erros.append(f"não foi possível ler {CSV_PATH}: {e}")

GRUPOS, e_csv = lib.parse_csv(texto, DEFAULTS) if texto else ([], [])
erros += e_csv
MODELOS_SYS = lib.system_ai_models(do)
erros += lib.validate_models(GRUPOS, MODELOS_SYS)

nao_zerados = lib.system_ai_not_zeroed(MODELOS_SYS)
if nao_zerados:
    avisos.append(f"{len(nao_zerados)} modelo(s) de system.ai sem o zero padrão — os limites por grupo só "
                  "funcionam com o zero aplicado. Rode o notebook zerar_system_ai.")

print(f"CSV: {CSV_PATH}")
if erros:
    print("❌ Corrija antes de continuar:\n- " + "\n- ".join(erros))
    dbutils.notebook.exit("ERRO_VALIDACAO")
print(f"✔ {len(GRUPOS)} grupo(s) válidos · modo = {'APLICAR' if APLICAR else 'SIMULAR (nada é alterado)'}")
for g in GRUPOS:
    print(f"  - {g['grupo']}: {len(g['emails'])} usuário(s), schema {CATALOGO}.{g['schema']}, warehouse {g['warehouse']}, "
          f"modelos {', '.join(g['modelos']) or '—'}, {lib.describe_limits(g['tpm'], g['qpm'])}, policy {g['usage_policy'] or '—'}, "
          f"tag {lib.TAG_KEY}={g['budget']}")
for a in avisos:
    print("⚠️ " + a)

# COMMAND ----------

# DBTITLE 1,1. Grupos e usuários
ETAPA = nova_etapa("1. grupos e usuários")
for g in GRUPOS:
    nome = g["grupo"]
    _, gid = registrar(ETAPA, nome, "grupo", lib.ensure_group, do, nome, APLICAR)
    registrar(ETAPA, nome, "atribuição ao workspace", lib.ensure_workspace_assignment, do, gid, APLICAR)
    for email in g["emails"]:
        st_u, uid = registrar(ETAPA, nome, f"usuário {email}", lib.ensure_user, do, email, APLICAR)
        if st_u == "ERRO":
            RESULTADO.append((ETAPA, nome, f"membro {email}", "pulado", "usuário não cadastrado (ver linha acima)"))
            continue
        registrar(ETAPA, nome, f"membro {email}", lib.ensure_member, do, gid, uid, APLICAR)
mostrar(ETAPA)

# COMMAND ----------

# DBTITLE 1,2. Schema e permissões
ETAPA = nova_etapa("2. schema e permissões")
for g in GRUPOS:
    registrar(ETAPA, g["grupo"], f"{CATALOGO}.{g['schema']}", lib.ensure_schema,
              do, CATALOGO, g["schema"], g["grupo"], GRUPO_ADMIN, APLICAR)
mostrar(ETAPA)

# COMMAND ----------

# DBTITLE 1,3. SQL warehouse
ETAPA = nova_etapa("3. SQL warehouse")
for g in GRUPOS:
    registrar(ETAPA, g["grupo"], g["warehouse"], lib.ensure_warehouse, do, g["warehouse"], g["grupo"], g["budget"], DEFAULTS, APLICAR)
mostrar(ETAPA)

# COMMAND ----------

# DBTITLE 1,4. GenAI (model services)
ETAPA = nova_etapa("4. GenAI")
for g in GRUPOS:
    if not g["modelos"]:
        RESULTADO.append((ETAPA, g["grupo"], "—", "pulado", "sem modelos no CSV (grupo sem GenAI)"))
    for m in g["modelos"]:
        st, _ = registrar(ETAPA, g["grupo"], f"modelo {m}", lib.ensure_model_service, do, CATALOGO, g["schema"],
                          g["grupo"], m, MODELOS_SYS[m]["model"], g["tpm"], g["qpm"], APLICAR)
        if st in ("criado", "atualizado", "atualizaria", "existe"):   # service existe → tag de custo
            full = f"{CATALOGO}.{g['schema']}.{lib.service_name(m)}"
            if st == "atualizaria" or APLICAR:
                registrar(ETAPA, g["grupo"], f"tag {m}", lib.ensure_tag, do, "model-services", full,
                          lib.TAG_KEY, g["budget"], APLICAR)
mostrar(ETAPA)

# COMMAND ----------

# DBTITLE 1,5. Serverless usage policy
ETAPA = nova_etapa("5. usage policy")
_rows = [r.asDict() for r in spark.sql(lib.POLICIES_SQL.format(dias=AUDIT_DIAS)).collect()]
POLICIES = lib.policies_from_audit(_rows)
ACCOUNT_ID = _rows[0]["account_id"] if _rows else None
for g in GRUPOS:
    nome_pol = g["usage_policy"]
    if not nome_pol:
        RESULTADO.append((ETAPA, g["grupo"], "—", "pulado", "sem usage_policy no CSV"))
        continue
    pid = POLICIES.get(nome_pol)
    if not pid or not ACCOUNT_ID:
        RESULTADO.append((ETAPA, g["grupo"], nome_pol, "ALERTA",
                          f"policy '{nome_pol}' não encontrada em system.access.audit (últimos {AUDIT_DIAS} dias). "
                          "Crie em Settings > Compute > Serverless usage policies ou aguarde o audit log "
                          "atualizar (pode levar algumas horas) e rode esta célula de novo."))
        continue
    registrar(ETAPA, g["grupo"], nome_pol, lib.ensure_policy_user, do, ACCOUNT_ID, pid, g["grupo"], APLICAR)
mostrar(ETAPA)

# COMMAND ----------

# DBTITLE 1,6. Resumo
if RESULTADO:
    display(spark.createDataFrame(RESULTADO, "etapa string, grupo string, item string, status string, detalhe string"))
n_err = sum(1 for r in RESULTADO if r[3] == "ERRO")
n_alerta = sum(1 for r in RESULTADO if r[3] == "ALERTA")
print(f"{'❌' if n_err else '⚠️' if n_alerta else '✔'} {len(RESULTADO)} item(ns) · {n_err} erro(s) · "
      f"{n_alerta} alerta(s) · modo {'aplicar' if APLICAR else 'simular'}")
for a in avisos:
    print("⚠️ " + a)
dbutils.notebook.exit(json.dumps({"modo": "aplicar" if APLICAR else "simular", "erros": n_err, "alertas": n_alerta,
                                  "avisos": avisos,
                                  "itens": [dict(zip(["etapa", "grupo", "item", "status", "detalhe"], r)) for r in RESULTADO]}))
