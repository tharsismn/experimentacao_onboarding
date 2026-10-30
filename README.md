# Onboarding de grupos — ambiente de experimentação

Notebook Databricks para cadastrar, a partir de um CSV, grupos e usuários, schemas com permissões fixas,
SQL warehouses, acesso a modelos de GenAI (Unity Gateway) e a associação dos grupos às serverless usage
policies — com a tag de custo `budget` em tudo o que é criado.

| Arquivo | Função |
|---|---|
| `onboarding_grupos.py` | Notebook, uma célula por etapa: 0 configuração e validação · 1 grupos e usuários · 2 schema e permissões · 3 SQL warehouse · 4 zerar modelos de `system.ai` (+ `enforce_ai_gateway_v3_only`) · 5 GenAI do grupo (model services + TPM/QPM) · 6 serverless usage policy · 7 resumo |
| `onboarding_lib.py` | Backend: funções idempotentes. As regras do ambiente chegam como parâmetros do bloco `CONFIG` do notebook |
| `grupos_exemplo.csv` | Modelo do CSV (e-mails `@example.com`) |
| `run_tests.sh [Classe[.teste]]` | Testes locais, sem workspace |

## Identidade
O notebook roda com a autenticação de quem o executa (o admin do ambiente). Não há service principal.
Por isso o notebook e o CSV ficam na **pasta pessoal do admin** (`/Workspace/Users/<admin>/experimentacao_onboarding`):
se ficassem em `Shared`, qualquer usuário poderia editar o código que roda com privilégio de admin.
O notebook **para** se a própria pasta estiver acessível aos grupos `users`/`account users`.

Quem executa: workspace admin, com `USE_CATALOG`/`CREATE_SCHEMA` no catálogo e `SELECT` em `system.access.audit`;
para a etapa 4, também **admin do metastore** (owner ou membro do grupo owner).

## CSV
```
grupo,emails,schema,warehouse,modelos,usage_policy,budget,tpm,qpm
fin_users,pessoa1@example.com;pessoa2@example.com,financas,wh_financas,gpt-5-mini;gte-large-en,exp_financas,foundationlab,,
```
| Coluna | Regra |
|---|---|
| `grupo`, `schema` | Obrigatórias; minúsculas, números e `_` |
| `emails`, `modelos` | Separados por `;`. `modelos` = nome do service em `system.ai`; vazio = grupo sem GenAI |
| `warehouse` | **Obrigatória**. Nome novo = o notebook cria o warehouse (serverless, com a tag `budget`); nome de um warehouse **já existente** = só concede `CAN_USE` ao grupo, sem alterar o warehouse nem as tags dele |
| `usage_policy` | **Nome** da serverless usage policy criada na interface; vazio = pula a etapa 6 |
| `budget` | Valor da tag de custo `budget`; vazio = nome do grupo. Use o mesmo valor na tag `budget` da usage policy |
| `tpm`, `qpm` | Limites do grupo por modelo; **vazio ou 0 = sem limite** |

## Instalação (pela interface do workspace)
1. Baixe `onboarding_grupos.py`, `onboarding_lib.py` e `grupos_exemplo.csv`.
2. No workspace, abra **Workspace → Home** (a sua pasta pessoal) e crie a pasta `experimentacao_onboarding`.
3. Na pasta, use **⋮ → Import** e arraste os dois `.py` **com o nome original**: o notebook entra como **notebook**
   e o `onboarding_lib.py` como **arquivo** — os dois precisam ficar na mesma pasta.
4. Ajuste o CSV, renomeie para `grupos.csv` e importe na mesma pasta.

Não há script de instalação nem job agendado: o notebook é executado manualmente pelo admin.

## Configuração do ambiente (célula 0, bloco `CONFIG`)
| Chave | Padrão | Controla |
|---|---|---|
| `privilegios_schema` | `USE_SCHEMA`, `SELECT`, `MODIFY`, `CREATE_TABLE`, `CREATE_FUNCTION`, `CREATE_MATERIALIZED_VIEW`, `EXECUTE`, `REFRESH` | Privilégios do grupo no schema da área (sem volumes) |
| `privilegios_catalogo` | `USE_CATALOG` | Privilégios do grupo no catálogo |
| `acessos_workspace` | `WORKSPACE_ACCESS`, `DATABRICKS_SQL_ACCESS` | O que o grupo usa no workspace |
| `tag_custo` | `budget` | Chave da tag de custo (warehouse e model services) |
| `warehouse` | 2X-Small, 1–3 clusters, auto-stop 5 min | Usado só quando o warehouse do CSV ainda não existe — sempre **serverless** |
| `ligar_enforce_gateway` | `True` | Etapa 4 liga `enforce_ai_gateway_v3_only` (bloqueia endpoints legados) |
| `audit_dias` | `30` | Janela de busca das usage policies no audit log (etapa 6) |

## Uso
1. Para cada área, crie a serverless usage policy na interface (Settings > Compute > Serverless usage policies)
   com a tag `budget=<área>`.
2. Abra `onboarding_grupos` e rode a célula **Parâmetros** (a primeira): os campos aparecem no topo. Informe: CSV (padrão `grupos.csv`), modo, catálogo (já existente),
   grupo admin e `system.ai` (`zerar` / `remover_zero` / `nao_mexer`). A etapa 4 concede ao executor o `MANAGE` em
   `system.ai` de que precisa (quem executa deve ser admin do metastore; se não for, a célula 0 para com o motivo).
3. Rode tudo em `simular`, confira, e depois em `aplicar`. Ao mudar um campo, rode a célula 0 de novo (ou Run all).
   Cada célula pode ser executada sozinha.

Reexecuções não duplicam nada: o que existe é reaproveitado, o que mudou no CSV é atualizado, o que é novo é criado.
Nada é removido — itens retirados do CSV continuam existindo (remoção manual).

A etapa 6 encontra a policy pelo nome em `system.access.audit` (últimos `audit_dias` dias); policies recém-criadas
podem levar algumas horas para aparecer, e policies mais antigas que a janela também geram ALERTA (aumente `audit_dias`).
Modelos novos publicados em `system.ai` entram sem o zero: a célula 0 avisa, e basta rodar a etapa 4 de novo.

## Validação com um usuário de área (não admin)
Admins e owners passam por cima dos grants — a validação precisa ser feita por um usuário comum da área:
1. criar tabela no schema da própria área → funciona;
2. criar tabela no schema de outra área → negado;
3. `CREATE VOLUME` → negado;
4. usar o warehouse da área → funciona; o de outra área → negado;
5. chamar o model service da área → funciona; `system.ai` direto → bloqueado (após ~5 min do zero);
6. endpoint legado `/serving-endpoints/databricks-*` → bloqueado.

## Limitações conhecidas
- `ai_query('system.ai.<modelo>', ...)` não respeita o zero e não aceita os model services dos grupos. Controle:
  budget do Unity Gateway (account console).
- Mudanças de rate limit levam ~5 min para valer em todas as chamadas.
- Serverless usage policies não têm API de workspace para criação: são criadas na interface
  (ou pela API de account, com billing admin). Budgets também são configurados no account console.
- Genie e Genie Code não passam pelo zero nem pelos limites por grupo (budget de Genie, por usuário).
