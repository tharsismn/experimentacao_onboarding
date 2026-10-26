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
CSV_COLUMNS = ["grupo", "emails", "schema", "warehouse", "modelos", "tpm", "qpm"]

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
        }
        for campo in ("tpm", "qpm"):
            raw = (row.get(campo) or "").strip()
            try:
                item[campo] = int(raw) if raw else int(defaults[f"{campo}_padrao"])
                if item[campo] < 0:
                    raise ValueError
            except ValueError:
                erros.append(f"{ctx}: {campo} inválido ('{raw}')")
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


def ensure_warehouse(do, nome, grupo, cfg, aplicar):
    whs = do("GET", "/api/2.0/sql/warehouses").get("warehouses", [])
    found = next((w for w in whs if w["name"] == nome), None)
    if found:
        wid, status = found["id"], "existe"
    elif not aplicar:
        return "criaria", nome
    else:
        wid = do("POST", "/api/2.0/sql/warehouses", {
            "name": nome, "cluster_size": cfg["warehouse_tamanho"], "min_num_clusters": 1,
            "max_num_clusters": cfg["warehouse_max_clusters"], "auto_stop_mins": cfg["warehouse_auto_stop"],
            "enable_serverless_compute": True, "warehouse_type": "PRO",
            "tags": {"custom_tags": [{"key": "grupo", "value": grupo}]},
        })["id"]
        status = "criado"
    if aplicar:
        do("PATCH", f"/api/2.0/permissions/warehouses/{wid}",
           {"access_control_list": [{"group_name": grupo, "permission_level": "CAN_USE"}]})
    return status, f"{nome} ({wid}) CAN_USE p/ {grupo}"


def group_rate_limits(grupo, tpm, qpm):
    base = {"key": "RATE_LIMIT_KEY_USER_GROUP", "principal": grupo, "renewal_period": "RATE_LIMIT_RENEWAL_PERIOD_MINUTE"}
    return [dict(base, tokens=tpm), dict(base, requests=qpm)]


def ensure_model_service(do, catalogo, schema, grupo, modelo, destino, tpm, qpm, aplicar):
    svc = service_name(modelo)
    full = f"{catalogo}.{schema}.{svc}"
    limits = group_rate_limits(grupo, tpm, qpm)
    try:
        do("GET", f"/api/2.1/unity-catalog/model-services/{full}")
        exists = True
    except ApiError as e:
        if not _not_found(e):
            raise
        exists = False
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
    return status, f"{full} → {modelo}; {tpm} TPM / {qpm} QPM p/ {grupo}"
