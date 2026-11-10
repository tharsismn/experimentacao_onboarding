"""Monitoramento de custos — ambiente de experimentação.

Backend do notebook monitoramento_setup (não depende do onboarding_grupos).

Camadas, todas no schema de monitoramento (`<catálogo>.<schema>`):
- materialized views filtradas pelo workspace (mv_*): cópia enxuta das system tables, com refresh agendado;
- cad_areas: as áreas (valor da tag de custo × grupo × warehouse), lidas do grupos.csv do onboarding;
- cad_membros: foto dos membros de cada área (usuário → budget), usada só para o Genie, que não tem tag;
- views de negócio (vw_*): custo por área = valor da tag de custo (ex.: budget), se for de uma área cadastrada; com
  filtro por linha (cada grupo vê só a tag da própria área; admins veem tudo). São as únicas que os grupos leem.

As funções idempotentes consultam antes de criar ou alterar. SQL passa por `sql(statement)` e chamadas de API
por `do(method, path, body=None)`; no notebook são spark.sql e o api_client do WorkspaceClient, nos testes, fakes.
"""
import csv
import io
import json
import re
from urllib.parse import quote

MVS = ["mv_billing", "mv_genai_uso"]
VIEWS = ["vw_custo", "vw_genai", "vw_areas"]   # as que os grupos leem
PLACEHOLDER_SCHEMA = "__SCHEMA_MONITORAMENTO__"
PLACEHOLDER_TITULO = "__TITULO__"
PLACEHOLDER_AREA = "__AREA__"   # só no modelo do dashboard da área

_IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")


class ApiError(Exception):
    def __init__(self, message, status=None, error_code=None):
        super().__init__(message)
        self.status = status
        self.error_code = error_code or ""


def _not_found(e):
    return (getattr(e, "status", None) == 404 or "NOT_FOUND" in getattr(e, "error_code", "")
            or "DOES_NOT_EXIST" in getattr(e, "error_code", "") or "does not exist" in str(e).lower())


def _lit(valor):
    """Literal SQL de string (aspas simples escapadas)."""
    return "'" + str(valor).replace("\\", "\\\\").replace("'", "\\'") + "'"


def valida_identificador(nome, campo):
    if not _IDENT_RE.match(nome or ""):
        raise ValueError(f"{campo} inválido: '{nome}' (use letras, números e _)")
    return nome


# ------------------------------------------------------------ materialized views ----

def mv_definitions(workspace_id):
    """SELECT de cada MV: só o filtro do workspace e as colunas usadas; nenhuma regra de negócio."""
    ws = _lit(workspace_id)
    return {
        "mv_billing": f"""SELECT u.usage_date, u.billing_origin_product AS produto, u.sku_name,
  u.usage_quantity AS quantidade, u.usage_quantity * p.pricing.default AS custo_lista_usd,
  u.identity_metadata.run_as AS run_as,
  u.usage_metadata.warehouse_id AS warehouse_id,
  coalesce(u.usage_metadata.ai_gateway.endpoint_name, u.usage_metadata.endpoint_name) AS endpoint,
  u.usage_metadata.ai_gateway.destination_model AS modelo,
  u.usage_metadata.genie.surface AS genie_surface,
  u.usage_metadata.notebook_id AS notebook_id,
  u.usage_metadata.job_id AS job_id,
  u.usage_metadata.dlt_pipeline_id AS pipeline_id,
  u.custom_tags
FROM system.billing.usage u
LEFT JOIN system.billing.list_prices p
  ON p.sku_name = u.sku_name AND p.cloud = u.cloud AND p.usage_unit = u.usage_unit AND p.currency_code = 'USD'
 AND u.usage_end_time >= p.price_start_time AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
WHERE u.workspace_id = {ws}""",
        "mv_genai_uso": f"""SELECT request_id, event_time, date(event_time) AS data, endpoint_name,
  coalesce(destination_model, model_metadata.destination_model) AS modelo,
  requester, total_tokens, status_code, service_tags
FROM system.ai_gateway.usage
WHERE workspace_id = {ws}""",
    }


def mv_comment(cron, fuso):
    # O comentário guarda o agendamento: é como a reexecução descobre que ele mudou.
    return f"Monitoramento de custos (refresh: {cron} {fuso})"


def create_mv_sql(full_name, select, cron, fuso):
    return (f"CREATE OR REPLACE MATERIALIZED VIEW {full_name}\n"
            f"SCHEDULE CRON {_lit(cron)} AT TIME ZONE {_lit(fuso)}\n"
            f"COMMENT {_lit(mv_comment(cron, fuso))}\nAS {select}")


def _norm(texto):
    return " ".join((texto or "").split())


def get_table(do, full_name):
    try:
        return do("GET", f"/api/2.1/unity-catalog/tables/{full_name}")
    except Exception as e:
        if _not_found(e):
            return None
        raise


def ensure_mv(do, sql, full_name, select, cron, fuso, aplicar):
    """Cria a MV; se já existir com a mesma definição e o mesmo agendamento, não mexe (recriar = recálculo
    completo). O refresh fica por conta do agendamento: um REFRESH manual colide com ele."""
    atual = get_table(do, full_name)
    igual = atual and _norm(atual.get("view_definition")) == _norm(select) \
        and (atual.get("comment") or "") == mv_comment(cron, fuso)
    if igual:
        return "existe", f"{full_name} (refresh {cron} {fuso})"
    acao = "atualiz" if atual else "cri"
    if not aplicar:
        return acao + "aria", f"{full_name} (refresh {cron} {fuso})"
    sql(create_mv_sql(full_name, select, cron, fuso))
    return acao + "ado", f"{full_name} (refresh {cron} {fuso})"


# ------------------------------------------------------------------ cadastro ----

def areas_do_csv(texto):
    """Áreas a partir do CSV do onboarding_grupos (grupos.csv): tag = coluna budget (vazia = nome do grupo, a mesma
    regra do onboarding), grupo = coluna grupo, warehouse = coluna warehouse. Devolve (areas, erros)."""
    leitor = csv.DictReader(io.StringIO((texto or "").lstrip("\ufeff")))
    faltam = [c for c in ("grupo", "warehouse") if c not in (leitor.fieldnames or [])]
    if faltam:
        return [], [f"colunas ausentes no CSV: {', '.join(faltam)}"]
    areas, erros = [], []
    for n, linha in enumerate(leitor, start=2):
        grupo = (linha.get("grupo") or "").strip()
        if not grupo:
            continue
        tag = (linha.get("budget") or "").strip() or grupo
        warehouse = (linha.get("warehouse") or "").strip()
        if not warehouse:
            erros.append(f"linha {n} ({grupo}): sem warehouse")
            continue
        outra = next((a for a in areas if a["tag"] == tag), None)
        if outra:
            erros.append(f"linha {n} ({grupo}): budget '{tag}' já é do grupo {outra['grupo']} — cada grupo deve ter "
                         "o próprio budget; corrija o CSV")
            continue
        areas.append({"tag": tag, "grupo": grupo, "warehouse": warehouse})
    if not areas and not erros:
        erros.append("CSV sem grupos")
    return areas, erros


def ensure_areas(query, sql, schema_full, areas, executor, tabela_existe, aplicar):
    """Regrava cad_areas com as áreas do CSV (só quando mudou). areas: dicts com tag, grupo, warehouse,
    warehouse_id. Tabela sem grants de grupos (só as views são lidas), então recriá-la não tira acessos."""
    chave = lambda a: (a["tag"], a["grupo"], a["warehouse"], a.get("warehouse_id") or "")
    atuais = sorted((r["budget"], r["grupo"], r["warehouse"], r.get("warehouse_id") or "")
                    for r in query(f"SELECT * FROM {schema_full}.cad_areas")) if tabela_existe else []
    det = ", ".join(f"{a['tag']}={a['grupo']}" for a in areas)
    if atuais == sorted(chave(a) for a in areas):
        return "existe", det
    acao = "atualiz" if tabela_existe else "cri"
    if not aplicar:
        return acao + "aria", det
    valores = ", ".join(f"({_lit(a['tag'])}, {_lit(a['grupo'])}, {_lit(a['warehouse'])}, {_lit(a.get('warehouse_id') or '')})"
                        for a in areas)
    sql(f"""CREATE OR REPLACE TABLE {schema_full}.cad_areas
COMMENT 'Áreas do monitoramento: valor da tag de custo, grupo que vê essa tag e warehouse do dashboard da área'
AS SELECT budget, grupo, warehouse, nullif(warehouse_id, '') AS warehouse_id,
  current_timestamp() AS atualizado_em, {_lit(executor)} AS atualizado_por
FROM VALUES {valores} AS v(budget, grupo, warehouse, warehouse_id)""")
    return acao + "ado", det


# -------------------------------------------------------------------- views ----

def admin_expr(grupo_admin):
    """Quem vê todas as linhas: admins do workspace (grupo local) e, se informado, o grupo admin de account."""
    expr = "is_member('admins')"
    return expr + (f" OR is_account_group_member({_lit(grupo_admin)})" if grupo_admin else "")


def view_definitions(schema_full, tag_custo, grupo_admin):
    """Área = a área de cad_areas com a mesma tag de custo da linha; para o Genie e as chamadas diretas a system.ai
    (sem tag), o budget do usuário em cad_membros. Cada linha só aparece para o grupo da área dela (is_account_group_member) e para os admins."""
    s, adm, tag = schema_full, admin_expr(grupo_admin), _lit(tag_custo)
    vw_custo = f"""SELECT b.usage_date AS data, coalesce(a.budget, m.budget) AS area, b.custom_tags[{tag}] AS tag,
  CASE WHEN a.budget IS NOT NULL THEN 'tag da área'
       WHEN m.budget IS NOT NULL THEN 'usuário da área'
       WHEN m.usuario IS NOT NULL THEN 'várias áreas'
       ELSE 'outros' END AS origem,
  lower(b.run_as) AS usuario, b.produto, b.sku_name, b.modelo, b.genie_surface,
  coalesce(b.endpoint, concat('warehouse ', b.warehouse_id), concat('job ', b.job_id),
           concat('notebook ', b.notebook_id), concat('pipeline ', b.pipeline_id), b.sku_name) AS recurso,
  b.quantidade, coalesce(b.custo_lista_usd, 0) AS custo_usd
FROM {s}.mv_billing b
LEFT JOIN {s}.cad_areas a ON a.budget = b.custom_tags[{tag}]
-- Genie e chamadas diretas a system.ai não têm tag: a área vem do usuário (membro de um único grupo de área)
LEFT JOIN {s}.cad_membros m ON (b.produto = 'GENIE' OR b.endpoint LIKE 'system.ai.%')
  AND b.custom_tags[{tag}] IS NULL AND m.usuario = lower(b.run_as)
WHERE is_account_group_member(coalesce(a.grupo, m.grupo)) OR {adm}"""
    vw_genai = f"""SELECT g.data,
  coalesce(a.budget, m.budget, CASE WHEN m.usuario IS NOT NULL THEN '(várias áreas)' END) AS area,
  g.service_tags[{tag}] AS tag,
  CASE WHEN g.endpoint_name LIKE 'system.ai.%' THEN 'system.ai direto'
       WHEN a.budget IS NOT NULL THEN 'tag da área'
       ELSE 'outros' END AS origem,
  lower(g.requester) AS usuario, g.endpoint_name, g.modelo,
  coalesce(g.total_tokens, 0) AS total_tokens, g.status_code,
  CASE WHEN g.status_code = 429 THEN 1 ELSE 0 END AS bloqueada_rate_limit
FROM {s}.mv_genai_uso g
LEFT JOIN {s}.cad_areas a ON a.budget = g.service_tags[{tag}]
-- chamadas diretas a system.ai não têm tag: a área vem do usuário
LEFT JOIN {s}.cad_membros m ON g.endpoint_name LIKE 'system.ai.%' AND m.usuario = lower(g.requester)
WHERE is_account_group_member(coalesce(a.grupo, m.grupo)) OR {adm}"""
    vw_areas = f"""SELECT budget AS area, grupo, warehouse, atualizado_em
FROM {s}.cad_areas
WHERE is_account_group_member(grupo) OR {adm}"""
    return {"vw_custo": vw_custo, "vw_genai": vw_genai, "vw_areas": vw_areas}


def ensure_view(do, sql, full_name, select, aplicar):
    atual = get_table(do, full_name)
    if atual and _norm(atual.get("view_definition")) == _norm(select):
        return "existe", full_name
    acao = "atualiz" if atual else "cri"
    if not aplicar:
        return acao + "aria", full_name
    sql(f"CREATE OR REPLACE VIEW {full_name}\nCOMMENT 'Monitoramento de custos — filtrada por área'\nAS {select}")
    return acao + "ado", full_name


# ------------------------------------------------------------------ membros ----

def _grupo_id(do, nome):
    r = do("GET", "/api/2.0/identity/groups?filter=" + quote(f'group_name eq "{nome}"'))
    grupos = r.get("groups", [])
    return grupos[0]["group_id"] if grupos else None


def membros_do_grupo(do, group_id, _visitados=None):
    """Identificadores (como aparecem no billing) dos membros do grupo, incluindo os de grupos dentro dele:
    e-mail dos usuários e application ID dos service principals."""
    visitados = _visitados if _visitados is not None else set()
    if group_id in visitados:
        return set()
    visitados.add(group_id)
    ids, token = set(), None
    while True:
        r = do("GET", f"/api/2.0/identity/groups/{group_id}/direct-members" + (f"?page_token={token}" if token else ""))
        for m in r.get("direct_group_members", []):
            tipo, pid = m.get("principal_type"), m.get("principal_id")
            if tipo == "USER":
                ids.add(do("GET", f"/api/2.0/identity/users/{pid}")["username"].lower())
            elif tipo == "SERVICE_PRINCIPAL":
                sp = do("GET", f"/api/2.0/identity/service-principals/{pid}")
                ids.add(str(sp.get("application_id") or sp.get("applicationId")).lower())
            elif tipo == "GROUP":
                ids |= membros_do_grupo(do, pid, visitados)
        token = r.get("next_page_token")
        if not token:
            return ids


def membros_por_usuario(do, areas):
    """Uma linha por usuário: o budget e o grupo da área dele, ou vazios se ele estiver em mais de uma área."""
    por_usuario = {}
    for a in areas:
        gid = _grupo_id(do, a["grupo"])
        for u in (membros_do_grupo(do, gid) if gid else set()):
            por_usuario.setdefault(u, []).append(a)
    return [{"usuario": u, "budget": l[0]["tag"] if len(l) == 1 else None,
             "grupo": l[0]["grupo"] if len(l) == 1 else None, "n_areas": len(l)}
            for u, l in sorted(por_usuario.items())]


def ensure_membros(query, sql, schema_full, linhas, executor, tabela_existe, aplicar):
    """Regrava cad_membros (foto atual dos membros) só quando mudou."""
    chave = lambda r: (r["usuario"], r.get("budget") or "", int(r["n_areas"]))
    atuais = sorted(chave(r) for r in query(f"SELECT * FROM {schema_full}.cad_membros")) if tabela_existe else None
    varias = sum(1 for r in linhas if r["n_areas"] > 1)
    det = f"{len(linhas)} usuário(s)" + (f", {varias} em mais de uma área" if varias else "")
    if atuais == sorted(chave(r) for r in linhas):
        return "existe", det
    acao = "atualiz" if tabela_existe else "cri"
    if not aplicar:
        return acao + "aria", det
    sql(f"""CREATE OR REPLACE TABLE {schema_full}.cad_membros (
  usuario STRING, budget STRING, grupo STRING, n_areas INT, atualizado_em TIMESTAMP, atualizado_por STRING)
COMMENT 'Foto dos membros das áreas (atualizada a cada execução do setup): liga o Genie, que não tem tag, ao budget'""")
    if linhas:
        lit = lambda v: _lit(v) if v else "NULL"
        sql(f"INSERT INTO {schema_full}.cad_membros VALUES " + ", ".join(
            f"({_lit(r['usuario'])}, {lit(r.get('budget'))}, {lit(r.get('grupo'))}, {int(r['n_areas'])}, "
            f"current_timestamp(), {_lit(executor)})" for r in linhas))
    return acao + "ado", det


# ------------------------------------------------------------------- grants ----

def _privilegios_atuais(do, securable, full_name, principal):
    try:
        perms = do("GET", f"/api/2.1/unity-catalog/permissions/{securable}/{full_name}?principal={quote(principal)}")
    except Exception as e:
        # grupo, schema ou view ainda não criados (instalação nova, modo simular): ainda não há permissões
        if "could not find principal" in str(e).lower() or _not_found(e):
            return set()
        raise
    return {p for a in perms.get("privilege_assignments", []) if a.get("principal") == principal
            for p in a.get("privileges", [])}


def ensure_grant(do, securable, full_name, principal, privileges, aplicar):
    faltam = [p for p in privileges if p not in _privilegios_atuais(do, securable, full_name, principal)]
    if not faltam:
        return "existe", f"{', '.join(privileges)} em {full_name}"
    if not aplicar:
        return "criaria", f"{', '.join(faltam)} em {full_name}"
    do("PATCH", f"/api/2.1/unity-catalog/permissions/{securable}/{full_name}",
       {"changes": [{"principal": principal, "add": faltam}]})
    return "criado", f"{', '.join(faltam)} em {full_name}"


# --------------------------------------------------------------- dashboards ----

def render_dashboard(template_text, schema_full, titulo, area=None):
    """Troca os marcadores do modelo — schema de monitoramento, título e, no modelo da área, a tag da área (dentro de
    um literal SQL, com as aspas escapadas) — campo a campo, no JSON já interpretado."""
    trocas = [(PLACEHOLDER_SCHEMA, schema_full), (PLACEHOLDER_TITULO, titulo)]
    if area is not None:
        trocas.append((PLACEHOLDER_AREA, area.replace("\\", "\\\\").replace("'", "\\'")))

    def trocar(v):
        if isinstance(v, str):
            for de, para in trocas:
                v = v.replace(de, para)
            return v
        if isinstance(v, list):
            return [trocar(x) for x in v]
        if isinstance(v, dict):
            return {k: trocar(x) for k, x in v.items()}
        return v
    return json.dumps(trocar(json.loads(template_text)), ensure_ascii=False)


def _json_ou_texto(texto):
    """O servidor guarda o JSON do dashboard compacto: compara o conteúdo, não a formatação."""
    try:
        return json.loads(texto or "")
    except ValueError:
        return texto


def _dashboard_path(pasta, nome):
    return f"{pasta.rstrip('/')}/{nome}.lvdash.json"


def find_dashboard_id(do, pasta, nome):
    try:
        st = do("GET", "/api/2.0/workspace/get-status?path=" + quote(_dashboard_path(pasta, nome)))
    except Exception as e:
        if _not_found(e):
            return None
        raise
    return st.get("resource_id")


def ensure_folder(do, pasta, aplicar):
    try:
        do("GET", "/api/2.0/workspace/get-status?path=" + quote(pasta))
        return "existe", pasta
    except Exception as e:
        if not _not_found(e):
            raise
    if not aplicar:
        return "criaria", pasta
    do("POST", "/api/2.0/workspace/mkdirs", {"path": pasta})
    return "criado", pasta


def ensure_dashboard(do, pasta, nome, serialized, warehouse_id, grupo, aplicar):
    """Cria ou atualiza o dashboard a partir do modelo e publica SEM credenciais embutidas: as consultas rodam
    com a identidade de quem vê, e o filtro por linha das views mostra só a área dele. `grupo` recebe CAN_RUN."""
    did = find_dashboard_id(do, pasta, nome)
    if did:
        atual = do("GET", f"/api/2.0/lakeview/dashboards/{did}")
        mudou = (_json_ou_texto(atual.get("serialized_dashboard")) != _json_ou_texto(serialized)
                 or atual.get("warehouse_id") != warehouse_id)
        try:
            pub = do("GET", f"/api/2.0/lakeview/dashboards/{did}/published")
        except Exception as e:
            if not _not_found(e):
                raise
            pub = None
        publicado = bool(pub) and pub.get("embed_credentials") is False and pub.get("warehouse_id") == warehouse_id
        status = "atualiz" if (mudou or not publicado) else None
    else:
        status = "cri"
    det = f"{nome} (warehouse {warehouse_id}, sem credenciais embutidas" + (f", CAN_RUN p/ {grupo})" if grupo else ")")
    if not aplicar:
        return (status + "aria" if status else "existe"), det
    if not did:
        did = do("POST", "/api/2.0/lakeview/dashboards",
                 {"display_name": nome, "parent_path": pasta, "serialized_dashboard": serialized,
                  "warehouse_id": warehouse_id})["dashboard_id"]
    elif mudou:
        do("PATCH", f"/api/2.0/lakeview/dashboards/{did}",
           {"display_name": nome, "serialized_dashboard": serialized, "warehouse_id": warehouse_id})
    if status:
        do("POST", f"/api/2.0/lakeview/dashboards/{did}/published",
           {"embed_credentials": False, "warehouse_id": warehouse_id})
    if grupo:
        do("PATCH", f"/api/2.0/permissions/dashboards/{did}",
           {"access_control_list": [{"group_name": grupo, "permission_level": "CAN_RUN"}]})
    return (status + "ado" if status else "existe"), det + f" · id {did}"


def warehouse_id_by_name(do, nome):
    whs = do("GET", "/api/2.0/sql/warehouses").get("warehouses", [])
    return next((w["id"] for w in whs if w["name"] == nome), None)


# ---------------------------------------------------------------- validação ----

BROAD_GROUPS = {"users", "account users"}


def broad_access_on_folder(do, folder):
    """Grupos amplos (users/account users) com permissão na pasta: o notebook roda com privilégio de admin,
    então a pasta dele deve ser acessível só ao admin."""
    oid = do("GET", "/api/2.0/workspace/get-status?path=" + quote(folder))["object_id"]
    acl = do("GET", f"/api/2.0/permissions/directories/{oid}").get("access_control_list", [])
    return sorted({a["group_name"] for a in acl if a.get("group_name") in BROAD_GROUPS})


def group_exists(do, nome):
    r = do("GET", "/api/2.0/identity/groups?filter=" + quote(f'group_name eq "{nome}"'))
    return bool(r.get("groups"))


def group_can_use_warehouse(do, warehouse_id, grupo):
    """O dashboard da área roda com a identidade de quem vê: o grupo precisa de CAN_USE (ou mais) no warehouse."""
    acl = do("GET", f"/api/2.0/permissions/warehouses/{warehouse_id}").get("access_control_list", [])
    return any(a.get("group_name") in (grupo, "users") and any(
        p.get("permission_level") in ("CAN_USE", "CAN_MONITOR", "CAN_MANAGE", "IS_OWNER") for p in a.get("all_permissions", []))
        for a in acl)
