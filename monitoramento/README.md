# Monitoramento de custos — ambiente de experimentação

Notebook Databricks que monta um monitoramento de custos do workspace: um **dashboard com todas as áreas** (para os
admins) e **um dashboard por área**, em que o grupo da área vê só os próprios custos. Independe do
`onboarding_grupos` e não altera nada dele.

| Arquivo | Função |
|---|---|
| `monitoramento_setup.py` | Notebook, uma célula por etapa: 0 configuração e validação · 1 schema e áreas · 2 materialized views · 3 views e acessos · 4 dashboards · 5 resumo |
| `monitoramento_lib.py` | Backend: funções idempotentes (consultam antes de criar ou alterar) |
| `dashboard_custos.lvdash.json` | Modelo do dashboard (o notebook troca o schema e o título e cria cada dashboard a partir dele) |

## Como a área de cada custo é definida
Pela **tag de custo `budget`** que o billing traz em cada linha (a tag que o onboarding aplica nos warehouses e model
services, e que deve estar também nas serverless usage policies). Só os valores informados no campo **6. Áreas** viram
área; o consumo com outro valor de tag aparece como "tag sem área" e o consumo sem a tag, como "sem tag" — os dois só
para os admins.

Custo sem a tag não entra em nenhuma área: warehouses já existentes precisam ter a tag `budget` aplicada, e as usage
policies também. Genie e storage não têm tag e ficam em "sem tag".

## Objetos criados (no schema de monitoramento)
| Objeto | Conteúdo |
|---|---|
| `mv_billing` | Materialized view de `system.billing.usage` × `system.billing.list_prices`, **filtrada por este workspace**: consumo com o custo a preço de tabela (US$) |
| `mv_genai_uso` | Materialized view de `system.ai_gateway.usage` filtrada por este workspace: chamadas e tokens de GenAI |
| `cad_areas` | As áreas do campo 6 (tag, grupo, warehouse) |
| `vw_custo`, `vw_genai`, `vw_areas` | MV + área (pela tag) com **filtro por linha**: cada linha só aparece para o grupo da área dela e para os admins |

As MVs têm refresh agendado (padrão: a cada 30 minutos, `refresh_cron` no `CONFIG`); cada refresh recalcula a MV
inteira e o custo dele aparece no próprio dashboard (como "sem tag"). Os grupos recebem `SELECT` **só nas views** —
nunca nas MVs, no cadastro ou nas system tables.

## Instalação (pela interface do workspace)
1. Baixe os três arquivos desta pasta.
2. Na sua pasta pessoal (**Workspace → Home**), crie a pasta `monitoramento` (por exemplo, dentro de
   `experimentacao_onboarding`) e importe os arquivos com o nome original: o `.py` do setup entra como **notebook**, os
   outros dois como **arquivos**. Os três precisam ficar na mesma pasta.

Não há script de instalação nem job: o notebook é executado manualmente pelo admin.

Quem executa: workspace admin, com `SELECT` em `system.billing` e `system.ai_gateway` e `USE_CATALOG`/`CREATE_SCHEMA`
no catálogo. O notebook **para** se a própria pasta estiver acessível aos grupos `users`/`account users`.

## Uso
1. Abra `monitoramento_setup` e rode a célula **Parâmetros** (a primeira): os campos aparecem no topo.
2. Preencha:

   | Campo | Exemplo |
   |---|---|
   | 1. Modo | `simular` (só mostra o que faria) ou `aplicar` |
   | 2. Catálogo | catálogo já existente |
   | 3. Schema de monitoramento | `monitoramento` |
   | 4. Warehouse dos admins | executa o setup e o dashboard geral |
   | 5. Grupo admin | opcional: grupo de account que vê todas as áreas (além dos admins do workspace) |
   | 6. Áreas | `financas=fin_users=wh_financas, riscos=risco_users=wh_riscos` |

   No campo 6, cada área é `tag=grupo=warehouse`: o valor da tag `budget`, o grupo que vê a área (precisa existir) e o
   warehouse em que roda o dashboard da área (o grupo precisa ter acesso a ele).
3. Rode em `simular`, confira e rode em `aplicar`. A última célula mostra os links dos dashboards.
4. Para incluir, remover ou mudar uma área, altere o campo 6 e rode de novo: nada é duplicado.

## Dashboards
Publicados **sem credenciais embutidas**: as consultas rodam com a identidade de quem vê, e o filtro por linha das
views mostra só a área dele. O dashboard de cada área roda no warehouse da área e tem `CAN_RUN` só para o grupo dela;
o geral roda no warehouse dos admins.

Páginas: **Visão geral** (custo no período, mês atual, projeção do mês, custo diário por produto, por área e por
produto) · **Por área** · **Por usuário** · **GenAI** (tokens, chamadas, bloqueios por rate limit e chamadas diretas a
`system.ai`). Filtros: período, área, usuário e produto.

## Configuração (célula 0, bloco `CONFIG`)
| Chave | Padrão | Controla |
|---|---|---|
| `tag_custo` | `budget` | Chave da tag que define a área |
| `refresh_cron` / `fuso` | `0 0/30 * * * ?` / `America/Sao_Paulo` | Agendamento do refresh das MVs |
| `pasta_dashboards` | `/Workspace/Users/{admin}/experimentacao_dashboards` | Onde os dashboards são criados |
| `titulo_admins` / `titulo_area` | — | Títulos dos dashboards |
