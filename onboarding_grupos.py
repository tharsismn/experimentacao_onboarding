# Databricks notebook source
# MAGIC %md
# MAGIC # Onboarding de grupos — ambiente de experimentação
# MAGIC
# MAGIC Lê um CSV (uma linha por grupo) e, para cada grupo, de forma idempotente:
# MAGIC 1. cria o **grupo de account** (se não existir), cadastra os usuários e atribui o grupo ao workspace;
# MAGIC 2. cria o **schema** do grupo no catálogo informado, com grants fixos (sem volumes);
# MAGIC 3. cria o **SQL warehouse serverless** (se não existir) e dá `CAN_USE` ao grupo;
# MAGIC 4. cria um **model service** por modelo no schema do grupo, com `EXECUTE` só p/ o grupo e limites TPM/QPM.
# MAGIC
# MAGIC **Como usar:** suba o CSV na **mesma pasta deste notebook** (padrão: `grupos.csv`), rode em `simular`,
# MAGIC confira o resultado e rode em `aplicar`. Pode ser re-executado. Também aceita um caminho completo.
# MAGIC
# MAGIC Colunas do CSV: `grupo,emails,schema,warehouse,modelos,tpm,qpm` — `emails` e `modelos` separados por `;`;
# MAGIC `warehouse`, `tpm` e `qpm` vazios usam o padrão abaixo. `modelos` = nome do service em `system.ai` (ex.: `gpt-5-mini`).
# MAGIC
# MAGIC Quem executa precisa ser workspace admin e ter `USE_CATALOG`/`CREATE_SCHEMA` (ou ser owner) no catálogo.

# COMMAND ----------

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
    "tpm_padrao": 20000,         # tokens/min por grupo, por modelo
    "qpm_padrao": 30,            # requisições/min por grupo, por modelo
}

APLICAR = dbutils.widgets.get("modo") == "aplicar"
CATALOGO = dbutils.widgets.get("catalogo").strip()
GRUPO_ADMIN = dbutils.widgets.get("grupo_admin").strip()

# COMMAND ----------

import os, sys
from urllib.parse import urlsplit, parse_qsl

_nb = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
NB_DIR = "/Workspace" + os.path.dirname(_nb)
sys.path.insert(0, NB_DIR)

# Nome simples → mesma pasta do notebook; caminho absoluto é usado como está
_csv = dbutils.widgets.get("csv_path").strip() or "grupos.csv"
CSV_PATH = _csv if _csv.startswith("/") else os.path.join(NB_DIR, _csv)
import onboarding_lib as lib

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import DatabricksError, NotFound, ResourceAlreadyExists, AlreadyExists

w = WorkspaceClient()


def do(method, path, body=None):
    """Adaptador: api_client do SDK → lib.ApiError com status normalizado."""
    parts = urlsplit(path)
    try:
        return w.api_client.do(method, parts.path, query=dict(parse_qsl(parts.query)) or None, body=body) or {}
    except DatabricksError as e:
        status = 404 if isinstance(e, NotFound) else 409 if isinstance(e, (ResourceAlreadyExists, AlreadyExists)) else None
        raise lib.ApiError(str(e), status=status, error_code=getattr(e, "error_code", "") or "") from e

# Os notebooks rodam com privilégio de admin: a pasta deles não pode ser editável por todos.
_amplos = lib.broad_access_on_folder(do, NB_DIR)
if _amplos:
    raise Exception(f"A pasta {NB_DIR} está acessível a {', '.join(_amplos)}. Publique os notebooks "
                    "na pasta pessoal do admin (/Workspace/Users/<admin>/...) — ver deploy.sh.")

# COMMAND ----------

# MAGIC %md ## Validação

# COMMAND ----------

erros = []
if not CATALOGO:
    erros.append("informe o catálogo")
else:
    try:
        do("GET", f"/api/2.1/unity-catalog/catalogs/{CATALOGO}")
    except lib.ApiError as e:
        erros.append(f"catálogo '{CATALOGO}' não encontrado ou sem acesso: {e}")
if GRUPO_ADMIN:
    st, _ = lib.ensure_group(do, GRUPO_ADMIN, aplicar=False)
    if st != "existe":
        erros.append(f"grupo admin '{GRUPO_ADMIN}' não existe no account")

try:
    texto = open(CSV_PATH, encoding="utf-8").read()
except Exception as e:
    texto = ""
    erros.append(f"não foi possível ler {CSV_PATH}: {e}")

grupos, e_csv = lib.parse_csv(texto, DEFAULTS) if texto else ([], [])
erros += e_csv
modelos_sys = lib.system_ai_models(do)
erros += lib.validate_models(grupos, modelos_sys)

if erros:
    print("❌ Corrija antes de continuar:\n- " + "\n- ".join(erros))
    dbutils.notebook.exit("ERRO_VALIDACAO")

print(f"CSV: {CSV_PATH}")
print(f"✔ {len(grupos)} grupo(s) válidos · modo = {'APLICAR' if APLICAR else 'SIMULAR (nada é alterado)'}")
for g in grupos:
    print(f"  - {g['grupo']}: {len(g['emails'])} usuário(s), schema {CATALOGO}.{g['schema']}, "
          f"warehouse {g['warehouse']}, modelos {', '.join(g['modelos']) or '—'}, {g['tpm']} TPM / {g['qpm']} QPM")

# COMMAND ----------

# MAGIC %md ## Execução

# COMMAND ----------

resultado = []


def etapa(grupo, nome, fn, *args):
    try:
        st, det = fn(*args)
        resultado.append((grupo, nome, st, "" if det is None else str(det)))
        return st, det
    except Exception as e:
        resultado.append((grupo, nome, "ERRO", str(e)[:500]))
        return "ERRO", None


for g in grupos:
    nome = g["grupo"]
    st, gid = etapa(nome, "grupo", lib.ensure_group, do, nome, APLICAR)
    etapa(nome, "workspace", lib.ensure_workspace_assignment, do, gid, APLICAR)
    for email in g["emails"]:
        st_u, uid = etapa(nome, f"usuário {email}", lib.ensure_user, do, email, APLICAR)
        etapa(nome, f"membro {email}", lib.ensure_member, do, gid, uid, APLICAR)
    etapa(nome, "schema + grants", lib.ensure_schema, do, CATALOGO, g["schema"], nome, GRUPO_ADMIN, APLICAR)
    etapa(nome, "warehouse", lib.ensure_warehouse, do, g["warehouse"], nome, DEFAULTS, APLICAR)
    for m in g["modelos"]:
        etapa(nome, f"modelo {m}", lib.ensure_model_service, do, CATALOGO, g["schema"], nome, m,
              modelos_sys[m]["model"], g["tpm"], g["qpm"], APLICAR)

df = spark.createDataFrame(resultado, "grupo string, etapa string, status string, detalhe string")
display(df)
n_err = sum(1 for r in resultado if r[2] == "ERRO")
print(f"{'❌' if n_err else '✔'} {len(resultado)} etapa(s), {n_err} erro(s)")

import json
dbutils.notebook.exit(json.dumps({"modo": "aplicar" if APLICAR else "simular", "erros": n_err,
                                  "etapas": [dict(zip(["grupo", "etapa", "status", "detalhe"], r)) for r in resultado]}))
