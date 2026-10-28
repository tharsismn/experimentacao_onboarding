"""Onboarding de grupos — ambiente de experimentação.

Funções idempotentes usadas pelos notebooks onboarding_grupos e zerar_system_ai.
Todas as chamadas à API passam por `do(method, path, body=None)`, que no notebook
é o api_client do WorkspaceClient e nos testes locais é um fake.
"""
import csv
import io
import re
from urllib.parse import quote

# Privilégios fixos do grupo no próprio schema (sem volumes).
SCHEMA_PRIVILEGES = [
    "USE_SCHEMA", "SELECT", "MODIFY", "CREATE_TABLE", "CREATE_FUNCTION",
    "CREATE_MATERIALIZED_VIEW", "EXECUTE", "REFRESH",
]
ENTITLEMENTS = ["WORKSPACE_ACCESS", "DATABRICKS_SQL_ACCESS"]
CSV_COLUMNS = ["grupo", "emails", "schema", "warehouse", "modelos"]
# Opcionais: nome da usage policy (interface) · valor da tag de custo · limites do grupo por modelo
# (tpm/qpm vazios ou 0 = sem limite — no Unity Gateway um limite 0 bloquearia o grupo inteiro)
CSV_OPTIONAL = ["usage_policy", "budget", "tpm", "qpm"]
POLICY_USER_ROLE = "roles/budgetPolicy.user"
TAG_KEY = "budget"   # tag de custo padronizada; o valor separa as áreas

_NAME_RE = re.compile(r"^[a-z0-9_]+$")
_EMAIL_RE = re.compile(r"^[^@\s;,]+@[^@\s;,]+\.[^@\s;,]+$")


class ApiError(Exception):
    """Erro de API com código HTTP/erro normalizados (preenchido pelo adaptador)."""

    def __init__(self, message, status=None, error_code=None):
        super().__init__(message)
        self.status = status
        self.error_code = error_code or ""


def _not_found(e):
    return (e.status == 404 or "NOT_FOUND" in e.error_code or "DOES_NOT_EXIST" in e.error_code
            or "does not exist" in str(e).lower() or "not directly assigned" in str(e).lower())


def _already_exists(e):
    return e.status == 409 or "ALREADY_EXISTS" in e.error_code or "already exists" in str(e).lower()


# ---------------------------------------------------------------- CSV ----

def _split(value):
    return [v.strip() for v in (value or "").split(";") if v.strip()]


def parse_csv(text, defaults):
    """Lê o CSV e devolve (grupos, erros). Cada grupo é um dict normalizado."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    missing = [c for c in CSV_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        return [], [f"colunas ausentes no CSV: {', '.join(missing)}"]

    grupos, erros, vistos = [], [], set()
    for n, row in enumerate(reader, start=2):
        g = (row.get("grupo") or "").strip()
        if not g:
            continue
        ctx = f"linha {n} ({g})"
        schema = (row.get("schema") or "").strip()
        emails = [e.lower() for e in _split(row.get("emails"))]
        modelos = _split(row.get("modelos"))
        item = {
            "grupo": g,
            "emails": emails,
            "schema": schema,
            "warehouse": (row.get("warehouse") or "").strip() or defaults["warehouse_padrao"],
            "modelos": modelos,
            "usage_policy": (row.get("usage_policy") or "").strip(),
            "budget": (row.get("budget") or "").strip() or g,
        }
        for campo in ("tpm", "qpm"):
            raw = (row.get(campo) or "").strip()
            try:
                valor = int(raw) if raw else 0
                if valor < 0:
                    raise ValueError
                item[campo] = valor or None   # None = sem limite
            except ValueError:
                erros.append(f"{ctx}: {campo} inválido ('{raw}') — use um inteiro positivo ou deixe vazio")
                item[campo] = None

        if not _NAME_RE.match(g):
            erros.append(f"{ctx}: nome de grupo deve ter só minúsculas, números e _")
        if g in vistos:
            erros.append(f"{ctx}: grupo repetido")
        vistos.add(g)
        if not _NAME_RE.match(schema):
            erros.append(f"{ctx}: schema obrigatório (minúsculas, números e _)")
        for e in emails:
            if not _EMAIL_RE.match(e):
                erros.append(f"{ctx}: e-mail inválido '{e}'")
        grupos.append(item)
    if not grupos and not erros:
        erros.append("CSV sem grupos")
    return grupos, erros


def validate_models(grupos, available):
    """Confere os modelos do CSV contra os services existentes em system.ai."""
    erros = []
    for g in grupos:
        for m in g["modelos"]:
            if m not in available:
                erros.append(f"{g['grupo']}: modelo '{m}' não existe em system.ai")
    return erros


def service_name(modelo):
    return "genai_" + re.sub(r"[^a-z0-9]+", "_", modelo.lower()).strip("_")


# ----------------------------------------------------- pasta do notebook ----

BROAD_GROUPS = {"users", "account users"}


def broad_access_on_folder(do, folder):
    """Grupos amplos (users/account users) com qualquer permissão na pasta — os notebooks
    rodam com privilégio de admin, então a pasta deve ser acessível só ao admin."""
    oid = do("GET", "/api/2.0/workspace/get-status?path=" + quote(folder))["object_id"]
    acl = do("GET", f"/api/2.0/permissions/directories/{oid}").get("access_control_list", [])
    return sorted({a["group_name"] for a in acl if a.get("group_name") in BROAD_GROUPS})


# ----------------------------------------------------------- system.ai ----

def system_ai_models(do):
    """{nome_curto: modelo_destino} dos services de system.ai (ex.: gpt-5-mini -> models/system.ai....)."""
    out, token = {}, None
    while True:
        q = "parent=schemas/system.ai&view=FULL&page_size=200" + (f"&page_token={token}" if token else "")
        r = do("GET", f"/api/2.1/unity-catalog/model-services?{q}")
        for s in r.get("model_services", []):
            short = s["name"].split("system.ai.", 1)[-1]
            dests = s.get("config", {}).get("routing", {}).get("destinations", [])
            model = (dests[0].get("pay_per_token_config", {}) or {}).get("model") if dests else None
            out[short] = {"model": model, "rate_limits": s.get("config", {}).get("rate_limits", []) or []}
        token = r.get("next_page_token")
        if not token:
            return out


ZERO_LIMITS = [
    {"key": "RATE_LIMIT_KEY_USER_DEFAULT", "renewal_period": "RATE_LIMIT_RENEWAL_PERIOD_MINUTE", "tokens": 0},
    {"key": "RATE_LIMIT_KEY_USER_DEFAULT", "renewal_period": "RATE_LIMIT_RENEWAL_PERIOD_MINUTE", "requests": 0},
]


def zero_limits_for(current):
    """Mantém limites que não são USER_DEFAULT e acrescenta o zero padrão. None se já está zerado."""
    # A listagem devolve os números como texto ("0"); o GET individual, como inteiro.
    defaults = [r for r in current if r.get("key") == "RATE_LIMIT_KEY_USER_DEFAULT"]
    is_zero = lambda v: v is not None and str(v) == "0"
    zerado = (any(is_zero(r.get("tokens")) for r in defaults) and any(is_zero(r.get("requests")) for r in defaults))
    if zerado:
        return None
    return [r for r in current if r.get("key") != "RATE_LIMIT_KEY_USER_DEFAULT"] + ZERO_LIMITS


# ------------------------------------------------------------- etapas ----
# Cada etapa devolve (status, detalhe). status: "criado" | "atualizado" | "existe" |
# "criaria" | "atualizaria" (modo simular). Erros sobem como exceção.

def ensure_group(do, nome, aplicar):
    r = do("GET", "/api/2.0/identity/groups?filter=" + quote(f'group_name eq "{nome}"'))
    groups = r.get("groups", [])
    if groups:
        return "existe", groups[0]["group_id"]
    if not aplicar:
        return "criaria", None
    g = do("POST", "/api/2.0/identity/groups", {"group_name": nome})
    return "criado", g["group_id"]


def ensure_user(do, email, aplicar):
    r = do("GET", "/api/2.0/identity/users?filter=" + quote(f'username eq "{email}"'))
    users = r.get("users", [])
    if users:
        return "existe", users[0]["user_id"]
    if not aplicar:
        return "criaria", None
    local = email.split("@")[0].split(".")
    body = {"username": email, "account_user_status": "ACTIVE",
            "full_name": {"given_name": local[0].title(), "family_name": " ".join(local[1:]).title() or local[0].title()}}
    u = do("POST", "/api/2.0/identity/users", body)
    return "criado", u["user_id"]


def group_member_ids(do, group_id):
    ids, token = set(), None
    while True:
        r = do("GET", f"/api/2.0/identity/groups/{group_id}/direct-members" + (f"?page_token={token}" if token else ""))
        ids |= {str(m["principal_id"]) for m in r.get("direct_group_members", [])}
        token = r.get("next_page_token")
        if not token:
            return ids


def ensure_member(do, group_id, user_id, aplicar):
    if group_id is None or user_id is None:
        return "criaria", None
    if str(user_id) in group_member_ids(do, group_id):
        return "existe", None
    if not aplicar:
        return "criaria", None
    try:
        do("POST", f"/api/2.0/identity/groups/{group_id}/direct-members", {"principal_id": int(user_id)})
        return "criado", None
    except ApiError as e:
        if _already_exists(e):
            return "existe", None
        raise


def ensure_workspace_assignment(do, principal_id, aplicar):
    if principal_id is None:
        return "criaria", None
    try:
        do("GET", f"/api/2.0/identity/workspace-assignments/{principal_id}")
        return "existe", None
    except ApiError as e:
        if not _not_found(e):
            raise
    if not aplicar:
        return "criaria", None
    do("POST", "/api/2.0/identity/workspace-assignments",
       {"principal_id": int(principal_id), "entitlements": ENTITLEMENTS})
    return "criado", ", ".join(ENTITLEMENTS)


def _grant(do, securable, full_name, principal, privileges):
    do("PATCH", f"/api/2.1/unity-catalog/permissions/{securable}/{full_name}",
       {"changes": [{"principal": principal, "add": privileges}]})


def ensure_schema(do, catalogo, schema, grupo, owner, aplicar):
    full = f"{catalogo}.{schema}"
    status = "existe"
    try:
        do("GET", f"/api/2.1/unity-catalog/schemas/{full}")
    except ApiError as e:
        if not _not_found(e):
            raise
        if not aplicar:
            return "criaria", full
        do("POST", "/api/2.1/unity-catalog/schemas",
           {"name": schema, "catalog_name": catalogo, "comment": f"Experimentação — grupo {grupo}"})
        status = "criado"
    if not aplicar:
        return status, full
    if owner:
        do("PATCH", f"/api/2.1/unity-catalog/schemas/{full}", {"owner": owner})
    _grant(do, "catalog", catalogo, grupo, ["USE_CATALOG"])
    _grant(do, "schema", full, grupo, SCHEMA_PRIVILEGES)
    return status, f"{full} (owner {owner or 'executor'}; grants: {', '.join(SCHEMA_PRIVILEGES)})"


def _with_tag(custom_tags, key, value):
    """Lista de tags com key=value (substitui só essa chave; mantém as demais)."""
    return [t for t in (custom_tags or []) if t.get("key") != key] + [{"key": key, "value": value}]


def ensure_warehouse(do, nome, grupo, budget, cfg, aplicar):
    whs = do("GET", "/api/2.0/sql/warehouses").get("warehouses", [])
    found = next((w for w in whs if w["name"] == nome), None)
    tag = f"{TAG_KEY}={budget}"
    if not found:
        if not aplicar:
            return "criaria", f"{nome} ({tag})"
        wid = do("POST", "/api/2.0/sql/warehouses", {
            "name": nome, "cluster_size": cfg["warehouse_tamanho"], "min_num_clusters": 1,
            "max_num_clusters": cfg["warehouse_max_clusters"], "auto_stop_mins": cfg["warehouse_auto_stop"],
            "enable_serverless_compute": True, "warehouse_type": "PRO",
            "tags": {"custom_tags": [{"key": TAG_KEY, "value": budget}]},
        })["id"]
        status = "criado"
    else:
        wid, status = found["id"], "existe"
        atual = do("GET", f"/api/2.0/sql/warehouses/{wid}")
        tags = (atual.get("tags") or {}).get("custom_tags", [])
        if {"key": TAG_KEY, "value": budget} not in [{"key": t.get("key"), "value": t.get("value")} for t in tags]:
            if not aplicar:
                return "atualizaria", f"{nome} ({wid}) tag {tag}"
            # edit regrava a configuração: reenvia a atual, mudando só as tags
            campos = ["name", "cluster_size", "min_num_clusters", "max_num_clusters", "auto_stop_mins",
                      "enable_serverless_compute", "warehouse_type", "spot_instance_policy", "enable_photon", "channel"]
            body = {k: atual[k] for k in campos if k in atual}
            body["tags"] = {"custom_tags": _with_tag(tags, TAG_KEY, budget)}
            do("POST", f"/api/2.0/sql/warehouses/{wid}/edit", body)
            status = "atualizado"
    if aplicar:
        do("PATCH", f"/api/2.0/permissions/warehouses/{wid}",
           {"access_control_list": [{"group_name": grupo, "permission_level": "CAN_USE"}]})
    return status, f"{nome} ({wid}) CAN_USE p/ {grupo}; tag {tag}"


def group_rate_limits(grupo, tpm, qpm):
    """Limites do grupo; tpm/qpm None = sem limite (nenhuma regra)."""
    base = {"key": "RATE_LIMIT_KEY_USER_GROUP", "principal": grupo, "renewal_period": "RATE_LIMIT_RENEWAL_PERIOD_MINUTE"}
    return ([dict(base, tokens=tpm)] if tpm else []) + ([dict(base, requests=qpm)] if qpm else [])


def describe_limits(tpm, qpm):
    return " / ".join([f"{tpm} TPM" if tpm else "TPM sem limite", f"{qpm} QPM" if qpm else "QPM sem limite"])


def _same_limits(a, b):
    """Compara listas de rate limit ignorando ordem e o tipo dos números ("0" vs 0)."""
    norm = lambda rs: sorted(tuple(sorted((k, str(v)) for k, v in r.items() if v is not None)) for r in rs or [])
    return norm(a) == norm(b)


def ensure_model_service(do, catalogo, schema, grupo, modelo, destino, tpm, qpm, aplicar):
    svc = service_name(modelo)
    full = f"{catalogo}.{schema}.{svc}"
    limits = group_rate_limits(grupo, tpm, qpm)
    try:
        atual = do("GET", f"/api/2.1/unity-catalog/model-services/{full}")
        exists = True
    except ApiError as e:
        if not _not_found(e):
            raise
        atual, exists = {}, False
    if exists and _same_limits((atual.get("config") or {}).get("rate_limits", []), limits):
        if aplicar:
            _grant(do, "model_service", full, grupo, ["EXECUTE"])
        return "existe", f"{full} → {modelo}; {describe_limits(tpm, qpm)} p/ {grupo}"
    if not aplicar:
        return ("atualizaria" if exists else "criaria"), full
    if exists:
        do("PATCH", f"/api/2.1/unity-catalog/model-services/{full}?update_mask=config.rate_limits",
           {"config": {"rate_limits": limits}})
        status = "atualizado"
    else:
        do("POST", f"/api/2.1/unity-catalog/model-services?model_service_id={svc}&parent=schemas/{catalogo}.{schema}", {
            "comment": f"GenAI do grupo {grupo} → {modelo}",
            "config": {
                "routing": {"destinations": [{
                    "name": "primary", "destination_type": "DESTINATION_TYPE_PAY_PER_TOKEN_FOUNDATION_MODEL",
                    "pay_per_token_config": {"model": destino}, "traffic_percentage": 100}]},
                "rate_limits": limits,
            },
        })
        status = "criado"
    _grant(do, "model_service", full, grupo, ["EXECUTE"])
    return status, f"{full} → {modelo}; {describe_limits(tpm, qpm)} p/ {grupo}"


# ------------------------------------------------- serverless usage policy ----
# Não há API de workspace que liste policies; o nome→id vem do audit log (system.access.audit),
# onde a criação registra policy_id e policy_name. Exclusões são descartadas.

POLICIES_SQL = """
SELECT account_id, action_name, event_time,
       to_json(request_params) AS request_params, response.result AS result
FROM system.access.audit
WHERE service_name = 'budgetPolicyCentral'
  AND action_name IN ('createBudgetPolicy', 'updateBudgetPolicy', 'deleteBudgetPolicy')
  AND response.status_code = 200
  AND event_date >= current_date() - INTERVAL {dias} DAYS
"""


def policies_from_audit(rows):
    """rows: dicts com action_name, event_time, request_params (json), result (json).
    Devolve {nome: policy_id} das policies existentes (último nome conhecido, sem as apagadas)."""
    import json as _json
    nome_por_id, apagadas = {}, set()
    for r in sorted(rows, key=lambda r: str(r["event_time"])):
        try:
            res = _json.loads(r.get("result") or "{}")
        except ValueError:
            res = {}
        if r["action_name"] == "deleteBudgetPolicy":
            pid = _json.loads(r.get("request_params") or "{}").get("policy_id")
            if pid:
                apagadas.add(pid)
            continue
        pid, nome = res.get("policy_id"), res.get("policy_name")
        if pid and nome:
            nome_por_id[pid] = nome
            apagadas.discard(pid)
    return {nome: pid for pid, nome in nome_por_id.items() if pid not in apagadas}


def ensure_policy_user(do, account_id, policy_id, grupo, aplicar):
    """Dá o papel User na policy ao grupo (rule set via proxy do workspace). PUT substitui
    o rule set inteiro, por isso lê as regras atuais e só acrescenta o grupo."""
    nome = f"accounts/{account_id}/budgetPolicies/{policy_id}/ruleSets/default"
    atual = do("GET", "/api/2.0/preview/accounts/access-control/rule-sets?name=" + quote(nome, safe="") + "&etag=")
    regras = [dict(r) for r in atual.get("grant_rules", [])]
    principal = f"groups/{grupo}"
    regra = next((r for r in regras if r.get("role") == POLICY_USER_ROLE), None)
    if regra and principal in regra.get("principals", []):
        return "existe", f"{grupo} já é User da policy {policy_id}"
    if not aplicar:
        return "criaria", f"{grupo} → User da policy {policy_id}"
    if regra:
        regra["principals"] = list(regra.get("principals", [])) + [principal]
    else:
        regras.append({"role": POLICY_USER_ROLE, "principals": [principal]})
    do("PUT", "/api/2.0/preview/accounts/access-control/rule-sets",
       {"name": nome, "rule_set": {"name": nome, "etag": atual.get("etag", ""), "grant_rules": regras}})
    return "criado", f"{grupo} → User da policy {policy_id}"


# ----------------------------------------------------------------- tags ----

def ensure_tag(do, entity_type, full_name, key, value, aplicar):
    tags = do("GET", f"/api/2.1/unity-catalog/entity-tag-assignments/{entity_type}/{full_name}/tags")
    atual = next((t for t in tags.get("tag_assignments", []) if t.get("tag_key") == key), None)
    if atual and atual.get("tag_value") == value:
        return "existe", f"{key}={value}"
    if not aplicar:
        return ("atualizaria" if atual else "criaria"), f"{key}={value}"
    if atual:
        do("PATCH", f"/api/2.1/unity-catalog/entity-tag-assignments/{entity_type}/{full_name}/tags/{key}?update_mask=tag_value",
           {"tag_value": value})
        return "atualizado", f"{key}={value}"
    do("POST", "/api/2.1/unity-catalog/entity-tag-assignments",
       {"entity_name": full_name, "entity_type": entity_type, "tag_key": key, "tag_value": value})
    return "criado", f"{key}={value}"


ENFORCE_GATEWAY_SETTING = "enforce_ai_gateway_v3_only"


def ensure_bool_setting(do, nome, valor, aplicar):
    """Setting booleana de workspace (API settings v2)."""
    atual = do("GET", f"/api/2.1/settings/{nome}")
    efetivo = ((atual.get("effective_boolean_val") or atual.get("boolean_val") or {}).get("value"))
    if efetivo == valor:
        return "sem mudança", f"{nome} = {valor}"
    if not aplicar:
        return "alteraria", f"{nome}: {efetivo} → {valor}"
    do("PATCH", f"/api/2.1/settings/{nome}", {"name": nome, "boolean_val": {"value": valor}})
    return "alterado", f"{nome}: {efetivo} → {valor}"


def system_ai_not_zeroed(servicos):
    """Modelos de system.ai ainda sem o zero padrão (para o aviso do onboarding)."""
    return sorted(n for n, i in servicos.items() if zero_limits_for(i["rate_limits"]) is not None)
