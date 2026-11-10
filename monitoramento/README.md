# Monitoramento de custos — ambiente de experimentação

Notebook Databricks que monta um monitoramento de custos do workspace: um **dashboard com todas as áreas** (para os
admins) e **um dashboard por área**, em que o grupo da área vê só os próprios custos. As áreas vêm do **mesmo
`grupos.csv` do `onboarding_grupos`**, que não é alterado.

| Arquivo | Função |
|---|---|
| `monitoramento_setup.py` | Notebook, uma célula por etapa: 0 configuração e validação · 1 schema e áreas · 2 materialized views · 3 views e acessos · 4 dashboards · 5 resumo |
| `monitoramento_lib.py` | Backend: funções idempotentes (consultam antes de criar ou alterar) |
| `dashboard_custos.lvdash.json` | Modelo do dashboard geral (todas as áreas, com filtro de área) |
| `dashboard_area.lvdash.json` | Modelo do dashboard de cada área: só os dados da área, sem filtro nem aba de área |

## Como a área de cada custo é definida
Pela **tag de custo `budget`** que o billing traz em cada linha (a tag que o onboarding aplica nos warehouses e model
services, e que deve estar também nas serverless usage policies). Cada linha do `grupos.csv` é uma área, com a tag da
coluna `budget` (vazia = nome do grupo, a mesma regra do onboarding); o consumo sem a tag ou com um valor que não é de nenhuma área aparece como "outros" — só para os admins.

Custo sem a tag não entra em nenhuma área: warehouses já existentes precisam ter a tag `budget` aplicada, e as usage
policies também. Storage não tem tag e fica em "outros"; o Genie e as chamadas diretas a `system.ai` (também sem tag) vão para a área do usuário (membro do grupo da área).

## Objetos criados (no schema de monitoramento)
| Objeto | Conteúdo |
|---|---|
| `mv_billing` | Materialized view de `system.billing.usage` × `system.billing.list_prices`, **filtrada por este workspace**: consumo com o custo a preço de tabela (US$) |
| `mv_genai_uso` | Materialized view de `system.ai_gateway.usage` filtrada por este workspace: chamadas e tokens de GenAI |
| `cad_areas` | As áreas do `grupos.csv` (tag, grupo, warehouse) |
| `vw_custo`, `vw_genai`, `vw_areas` | MV + área (pela tag) com **filtro por linha**: cada linha só aparece para o grupo da área dela e para os admins |

As MVs têm refresh agendado (padrão: a cada 30 minutos, `refresh_cron` no `CONFIG`); cada refresh recalcula a MV
inteira e o custo dele aparece no próprio dashboard (como "outros"). Os grupos recebem `SELECT` **só nas views** —
nunca nas MVs, no cadastro ou nas system tables.

## Instalação (pela interface do workspace)
1. Baixe os quatro arquivos desta pasta.
2. Na sua pasta pessoal (**Workspace → Home**), crie a pasta `monitoramento` dentro de `experimentacao_onboarding`
   (assim o `../grupos.csv` do onboarding é encontrado) e importe os quatro arquivos com o nome original: o `.py` do
   setup entra como **notebook**, a lib como **arquivo** e os dois `.lvdash.json` aparecem como dashboards — são só
   modelos, lidos pelo notebook (não os abra: as consultas deles têm marcadores no lugar do schema).

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
   | 5. Grupo admin | **obrigatório**: grupo de account que acessa o dashboard geral e vê todas as áreas (além dos admins do workspace) |
   | 6. CSV do onboarding | `../grupos.csv` (padrão: o CSV da pasta do onboarding) |

   Do CSV, o setup usa três colunas: `budget` (a tag da área; vazia = nome do grupo), `grupo` (quem vê a área) e
   `warehouse` (onde roda o dashboard da área; o onboarding já deu acesso ao grupo). Cada grupo deve ter o próprio
   `budget`: dois grupos com o mesmo valor param a validação.
3. Rode em `simular`, confira e rode em `aplicar`. A última célula mostra os links dos dashboards.
4. Sempre que o `grupos.csv` mudar (área nova, warehouse trocado), rode o onboarding e depois este setup: nada é
   duplicado.

## Dashboards
Publicados **sem credenciais embutidas**: as consultas rodam com a identidade de quem vê, e o filtro por linha das
views mostra só o que ela pode ver.

| Dashboard | Quem acessa | Conteúdo |
|---|---|---|
| Geral (todas as áreas) | só o **grupo admin** (campo 5); roda no warehouse dos admins | Visão geral · Por área · Por usuário · GenAI · Genie; filtros de período, área, usuário e produto. Mostra também "(outros)" e "(várias áreas)" |
| Custos — `<tag>` (um por área) | só o **grupo da área**; roda no warehouse da área | Só os dados da área (fixos no modelo, mesmo para um admin que o abra): Visão geral (com os recursos mais caros) · Por usuário · GenAI · Genie; filtros de período, usuário e produto |

Os dois têm custo no período selecionado e do mês atual (MTD), busca por usuário nas tabelas, GenAI (custo, tokens,
chamadas, bloqueios por rate limit e acesso direto aos modelos de IA) e Genie acima da cota gratuita, por SKU.

## Configuração (célula 0, bloco `CONFIG`)
| Chave | Padrão | Controla |
|---|---|---|
| `tag_custo` | `budget` | Chave da tag que define a área |
| `refresh_cron` / `fuso` | `0 0/30 * * * ?` / `America/Sao_Paulo` | Agendamento do refresh das MVs |
| `pasta_dashboards` | `/Workspace/Users/{admin}/experimentacao_dashboards` | Onde os dashboards são criados |
| `titulo_admins` / `titulo_area` | — | Títulos dos dashboards |
