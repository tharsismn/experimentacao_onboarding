# Databricks notebook source
# MAGIC %md
# MAGIC # Zerar o uso padrão dos modelos de `system.ai`
# MAGIC
# MAGIC Aplica rate limit **0 (tokens e requisições) por usuário** em todos os model services de `system.ai`,
# MAGIC sem remover nenhum grant. Os grupos usam os model services próprios criados pelo `onboarding_grupos`,
# MAGIC que não são afetados. Modelos novos publicados pela Databricks entram sem limite — por isso este
# MAGIC notebook roda também como job diário.
# MAGIC
# MAGIC Em `aplicar`, também liga a configuração de workspace **`enforce_ai_gateway_v3_only`**, que bloqueia os
# MAGIC endpoints legados `/serving-endpoints/databricks-*` (e o `ai_query` com nomes legados), forçando todo acesso
# MAGIC a modelos pelo Unity Gateway. `remover_zero` não desliga essa configuração.
# MAGIC
# MAGIC **Limitação conhecida:** `ai_query('system.ai.<modelo>', ...)` não respeita o zero. Para ele o controle é o
# MAGIC budget do Unity Gateway (account console).
# MAGIC
# MAGIC Quem executa precisa de `MANAGE` em `system.ai` (o metastore admin pode conceder a si mesmo — opção abaixo).

# COMMAND ----------

dbutils.widgets.dropdown("modo", "simular", ["simular", "aplicar", "remover_zero"], "1. Modo")
dbutils.widgets.dropdown("conceder_manage", "nao", ["nao", "sim"], "2. Conceder MANAGE em system.ai ao executor")
MODO = dbutils.widgets.get("modo")
CONCEDER = dbutils.widgets.get("conceder_manage") == "sim"

# COMMAND ----------

import os, sys
from urllib.parse import urlsplit, parse_qsl

_nb = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
NB_DIR = "/Workspace" + os.path.dirname(_nb)
sys.path.insert(0, NB_DIR)
import onboarding_lib as lib

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import DatabricksError, NotFound

w = WorkspaceClient()


def do(method, path, body=None):
    parts = urlsplit(path)
    try:
        return w.api_client.do(method, parts.path, query=dict(parse_qsl(parts.query, keep_blank_values=True)) or None, body=body) or {}
    except DatabricksError as e:
        raise lib.ApiError(str(e), status=404 if isinstance(e, NotFound) else None,
                           error_code=getattr(e, "error_code", "") or "") from e

# Os notebooks rodam com privilégio de admin: a pasta deles não pode ser editável por todos.
_amplos = lib.broad_access_on_folder(do, NB_DIR)
if _amplos:
    raise Exception(f"A pasta {NB_DIR} está acessível a {', '.join(_amplos)}. Publique os notebooks "
                    "na pasta pessoal do admin (/Workspace/Users/<admin>/...) — ver deploy.sh.")

# COMMAND ----------

if CONCEDER and MODO != "simular":
    eu = w.current_user.me().user_name
    do("PATCH", "/api/2.1/unity-catalog/permissions/schema/system.ai",
       {"changes": [{"principal": eu, "add": ["MANAGE"]}]})
    print(f"MANAGE em system.ai concedido a {eu}")

servicos = lib.system_ai_models(do)
resultado = []
if MODO != "remover_zero":
    try:
        st, det = lib.ensure_bool_setting(do, lib.ENFORCE_GATEWAY_SETTING, True, MODO == "aplicar")
        resultado.append((f"(configuração) {lib.ENFORCE_GATEWAY_SETTING}", st, det))
    except Exception as e:
        resultado.append((f"(configuração) {lib.ENFORCE_GATEWAY_SETTING}", "ERRO", str(e)[:300]))
for nome, info in sorted(servicos.items()):
    atual = info["rate_limits"]
    if MODO == "remover_zero":
        novo = [r for r in atual if r.get("key") != "RATE_LIMIT_KEY_USER_DEFAULT"]
        novo = None if novo == atual else novo
    else:
        novo = lib.zero_limits_for(atual)
    if novo is None:
        resultado.append((nome, "sem mudança", ""))
        continue
    if MODO == "simular":
        resultado.append((nome, "alteraria", ""))
        continue
    try:
        do("PATCH", f"/api/2.1/unity-catalog/model-services/system.ai.{nome}?update_mask=config.rate_limits",
           {"config": {"rate_limits": novo}})
        resultado.append((nome, "alterado", ""))
    except Exception as e:
        resultado.append((nome, "ERRO", str(e)[:300]))

display(spark.createDataFrame(resultado, "modelo string, status string, detalhe string"))
n_err = sum(1 for r in resultado if r[1] == "ERRO")
print(f"{len(resultado)} modelo(s) · {sum(1 for r in resultado if r[1] in ('alterado', 'alteraria'))} "
      f"{'alterado(s)' if MODO != 'simular' else 'a alterar'} · {n_err} erro(s)")
if n_err:
    raise Exception(f"{n_err} modelo(s) com erro — veja a tabela")

import json
from collections import Counter
dbutils.notebook.exit(json.dumps({"modo": MODO, "contagem": Counter(r[1] for r in resultado)}))
