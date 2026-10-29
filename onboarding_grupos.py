# Databricks notebook source
# MAGIC %md
# MAGIC # Onboarding de grupos — ambiente de experimentação
# MAGIC
# MAGIC **Primeiro rode a célula "Parâmetros"**: os campos aparecem no topo do notebook. Preencha-os e rode a
# MAGIC **célula 0 (configuração e validação)** e as etapas em ordem (ou **Run all**). Ao mudar um campo, rode a célula 0
# MAGIC de novo. Cada etapa pode ser executada de novo sozinha — nada é duplicado.
# MAGIC
# MAGIC | Etapa | O que faz |
# MAGIC |---|---|
# MAGIC | 1. Grupos e usuários | grupo de account, cadastro dos usuários, membros, atribuição ao workspace |
# MAGIC | 2. Schema e permissões | schema no catálogo informado, owner = grupo admin, privilégios da configuração (sem volumes) |
# MAGIC | 3. SQL warehouse | warehouse serverless com a tag de custo, `CAN_USE` p/ o grupo |
# MAGIC | 4. Zerar modelos de system.ai | limite 0 para **todos** em todos os modelos de `system.ai` + só acesso via Unity Gateway |
# MAGIC | 5. GenAI do grupo | model service da área por modelo do CSV (`EXECUTE` só p/ o grupo, tag de custo), com o TPM/QPM do CSV |
# MAGIC | 6. Serverless usage policy | papel **User** do grupo na policy informada (a policy é criada antes, na interface) |
# MAGIC | 7. Resumo | tabela com todas as etapas executadas |
# MAGIC
# MAGIC **CSV** (na mesma pasta deste notebook): `grupo,emails,schema,warehouse,modelos,usage_policy,budget,tpm,qpm` —
# MAGIC `emails` e `modelos` separados por `;`; `warehouse` vazio usa o padrão; `tpm`/`qpm` vazios ou 0 = **sem limite**;
# MAGIC `usage_policy` = **nome** da serverless usage policy criada em Settings > Compute (vazio = pula a etapa 6);
# MAGIC `budget` = valor da tag de custo da área (vazio = nome do grupo). Use o **mesmo** valor na tag da usage policy,
# MAGIC para o custo do serverless, do SQL e do GenAI somar na mesma área.
# MAGIC
# MAGIC Quem executa: workspace admin, com `USE_CATALOG`/`CREATE_SCHEMA` no catálogo, `SELECT` em `system.access.audit`
# MAGIC e, para a etapa 4, metastore admin.

# COMMAND ----------

# DBTITLE 1,Parâmetros (rode primeiro: os campos aparecem no topo do notebook)
dbutils.widgets.text("csv_path", "grupos.csv", "1. CSV (nome na pasta do notebook ou caminho completo)")
dbutils.widgets.dropdown("modo", "simular", ["simular", "aplicar"], "2. Modo")
dbutils.widgets.text("catalogo", "", "3. Catálogo (já existente)")
dbutils.widgets.text("grupo_admin", "", "4. Grupo admin (owner dos schemas; opcional)")
dbutils.widgets.dropdown("system_ai", "zerar", ["zerar", "remover_zero", "nao_mexer"], "5. Etapa 4: system.ai")
dbutils.widgets.dropdown("conceder_manage", "nao", ["nao", "sim"], "6. Etapa 4: conceder MANAGE em system.ai")

_faltando = [] if dbutils.widgets.get("catalogo").strip() else ["3. Catálogo"]
print("Campos prontos no topo do notebook. Preencha-os e rode as próximas células (ou Run all).")
print("⚠️ Obrigatório e ainda vazio: " + ", ".join(_faltando) if _faltando else "✔ Campos obrigatórios preenchidos.")

# COMMAND ----------

# DBTITLE 1,0. Configuração e validação
# ---------------------------------------------------------------- configuração do ambiente ----
# Regras aplicadas a TODOS os grupos. Altere aqui (e não na onboarding_lib.py).
CONFIG = {
    # Privilégios do grupo no schema da própria área (sem volumes: nenhum *_VOLUME)
    "privilegios_schema": ["USE_SCHEMA", "SELECT", "MODIFY", "CREATE_TABLE", "CREATE_FUNCTION",
                           "CREATE_MATERIALIZED_VIEW", "EXECUTE", "REFRESH"],
    # Privilégios do grupo no catálogo
    "privilegios_catalogo": ["USE_CATALOG"],
    # O que o grupo pode usar no workspace
    "acessos_workspace": ["WORKSPACE_ACCESS", "DATABRICKS_SQL_ACCESS"],
    # Tag de custo aplicada ao warehouse e aos model services (o valor vem da coluna budget do CSV)
    "tag_custo": "budget",
    # SQL warehouse criado para a área — sempre serverless (só vale na criação; existentes não são reconfigurados)
    "warehouse": {"padrao": "wh_experimentacao", "tamanho": "2X-Small",
                  "min_clusters": 1, "max_clusters": 3, "auto_stop_min": 5},
    # Etapa 4: além do zero, obrigar todo acesso a modelos a passar pelo Unity Gateway
    # (bloqueia os endpoints legados /serving-endpoints/databricks-*)
    "ligar_enforce_gateway": True,
    # Etapa 6: quantos dias para trás procurar a criação das usage policies no audit log
    "audit_dias": 30,
}

APLICAR = dbutils.widgets.get("modo") == "aplicar"
CATALOGO = dbutils.widgets.get("catalogo").strip()
GRUPO_ADMIN = dbutils.widgets.get("grupo_admin").strip()
SYSTEM_AI = dbutils.widgets.get("system_ai")
CONCEDER_MANAGE = dbutils.widgets.get("conceder_manage") == "sim"

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
        return w.api_client.do(method, parts.path, query=dict(parse_qsl(parts.query, keep_blank_values=True)) or None,
                               body=body) or {}
    except DatabricksError as e:
        status = 404 if isinstance(e, NotFound) else 409 if isinstance(e, (ResourceAlreadyExists, AlreadyExists)) else None
        raise lib.ApiError(str(e), status=status, error_code=getattr(e, "error_code", "") or "") from e


# O notebook roda com privilégio de admin: a pasta dele não pode ser editável por todos.
_amplos = lib.broad_access_on_folder(do, NB_DIR)
if _amplos:
    raise Exception(f"A pasta {NB_DIR} está acessível a {', '.join(_amplos)}. Importe o notebook "
                    "na pasta pessoal do admin (/Workspace/Users/<admin>/...).")

RESULTADO = []   # (etapa, grupo, item, status, detalhe) — acumulado pelas células 1 a 6


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
    erros.append("preencha o campo '3. Catálogo' no topo do notebook (catálogo já existente) e rode de novo")
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

GRUPOS, e_csv = lib.parse_csv(texto, CONFIG["warehouse"]["padrao"]) if texto else ([], [])
erros += e_csv
MODELOS_SYS = lib.system_ai_models(do)
erros += lib.validate_models(GRUPOS, MODELOS_SYS)

nao_zerados = lib.system_ai_not_zeroed(MODELOS_SYS)
if nao_zerados:
    avisos.append(f"{len(nao_zerados)} modelo(s) de system.ai sem o zero (ex.: {', '.join(nao_zerados[:5])}"
                  f"{'…' if len(nao_zerados) > 5 else ''}). A etapa 4 com 'system.ai = zerar' resolve.")

print(f"CSV: {CSV_PATH}")
if erros:
    # Erro (e não dbutils.notebook.exit): a mensagem aparece na célula e o "Run all" para aqui.
    raise Exception("Validação falhou — corrija antes de continuar (nada foi alterado):\n- " + "\n- ".join(erros))
print(f"✔ {len(GRUPOS)} grupo(s) válidos · modo = {'APLICAR' if APLICAR else 'SIMULAR (nada é alterado)'} · "
      f"etapa 4 = {SYSTEM_AI}")
for g in GRUPOS:
    print(f"  - {g['grupo']}: {len(g['emails'])} usuário(s), schema {CATALOGO}.{g['schema']}, warehouse {g['warehouse']}, "
          f"modelos {', '.join(g['modelos']) or '—'}, {lib.describe_limits(g['tpm'], g['qpm'])}, "
          f"policy {g['usage_policy'] or '—'}, tag {CONFIG['tag_custo']}={g['budget']}")
for a in avisos:
    print("⚠️ " + a)

# COMMAND ----------

# DBTITLE 1,1. Grupos e usuários
ETAPA = nova_etapa("1. grupos e usuários")
for g in GRUPOS:
    nome = g["grupo"]
    _, gid = registrar(ETAPA, nome, "grupo", lib.ensure_group, do, nome, APLICAR)
    registrar(ETAPA, nome, "atribuição ao workspace", lib.ensure_workspace_assignment, do, gid,
              CONFIG["acessos_workspace"], APLICAR)
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
    registrar(ETAPA, g["grupo"], f"{CATALOGO}.{g['schema']}", lib.ensure_schema, do, CATALOGO, g["schema"], g["grupo"],
              GRUPO_ADMIN, CONFIG["privilegios_schema"], CONFIG["privilegios_catalogo"], APLICAR)
mostrar(ETAPA)

# COMMAND ----------

# DBTITLE 1,3. SQL warehouse
ETAPA = nova_etapa("3. SQL warehouse")
for g in GRUPOS:
    registrar(ETAPA, g["grupo"], g["warehouse"], lib.ensure_warehouse, do, g["warehouse"], g["grupo"],
              CONFIG["tag_custo"], g["budget"], CONFIG["warehouse"], APLICAR)
mostrar(ETAPA)

# COMMAND ----------

# DBTITLE 1,4. Zerar modelos de system.ai (todos os usuários)
# Limite 0 por usuário em TODOS os modelos de system.ai, sem remover permissões. Os grupos usam os model
# services próprios (etapa 5), que não são afetados. Modelos novos publicados pela Databricks entram sem
# limite: rode esta etapa de novo quando a célula 0 avisar.
ETAPA = nova_etapa("4. zerar system.ai")
if SYSTEM_AI == "nao_mexer":
    RESULTADO.append((ETAPA, "(todos)", "system.ai", "pulado", "parâmetro 'system.ai' = nao_mexer"))
else:
    if CONCEDER_MANAGE:
        registrar(ETAPA, "(todos)", "MANAGE em system.ai", lib.grant_manage_system_ai,
                  do, w.current_user.me().user_name, APLICAR)
    if SYSTEM_AI == "zerar" and CONFIG["ligar_enforce_gateway"]:
        registrar(ETAPA, "(todos)", lib.ENFORCE_GATEWAY_SETTING, lib.ensure_bool_setting,
                  do, lib.ENFORCE_GATEWAY_SETTING, True, APLICAR)
    for modelo, st, det in lib.apply_system_ai(do, lib.system_ai_models(do), SYSTEM_AI, APLICAR):
        RESULTADO.append((ETAPA, "(todos)", modelo, st, det))
mostrar(ETAPA)

# COMMAND ----------

# DBTITLE 1,5. GenAI do grupo (model services + TPM/QPM)
# Para cada modelo do CSV: model service da área <catálogo>.<schema>.genai_<modelo>, com EXECUTE só para o
# grupo, tag de custo e o TPM/QPM do CSV (vazio ou 0 = sem limite). Só regrava se algo mudou.
ETAPA = nova_etapa("5. GenAI do grupo")
for g in GRUPOS:
    if not g["modelos"]:
        RESULTADO.append((ETAPA, g["grupo"], "—", "pulado", "sem modelos no CSV (grupo sem GenAI)"))
    for m in g["modelos"]:
        st, _ = registrar(ETAPA, g["grupo"], f"modelo {m}", lib.ensure_model_service, do, CATALOGO, g["schema"],
                          g["grupo"], m, MODELOS_SYS[m]["model"], g["tpm"], g["qpm"], APLICAR)
        if st in ("criado", "atualizado", "existe", "atualizaria"):   # service existe → tag de custo
            registrar(ETAPA, g["grupo"], f"tag {m}", lib.ensure_tag, do, "model-services",
                      f"{CATALOGO}.{g['schema']}.{lib.service_name(m)}", CONFIG["tag_custo"], g["budget"], APLICAR)
mostrar(ETAPA)

# COMMAND ----------

# DBTITLE 1,6. Serverless usage policy
ETAPA = nova_etapa("6. usage policy")
_rows = [r.asDict() for r in spark.sql(lib.POLICIES_SQL.format(dias=CONFIG["audit_dias"])).collect()]
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
                          f"policy '{nome_pol}' não encontrada em system.access.audit (últimos {CONFIG['audit_dias']} dias). "
                          "Crie em Settings > Compute > Serverless usage policies, aguarde o audit log atualizar "
                          "(pode levar algumas horas) ou aumente 'audit_dias' na configuração, e rode esta célula de novo."))
        continue
    registrar(ETAPA, g["grupo"], nome_pol, lib.ensure_policy_user, do, ACCOUNT_ID, pid, g["grupo"], APLICAR)
mostrar(ETAPA)

# COMMAND ----------

# DBTITLE 1,7. Resumo
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
