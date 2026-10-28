# Onboarding de grupos — ambiente de experimentação

Notebooks Databricks para cadastrar, a partir de um CSV, grupos e usuários, schemas com permissões fixas,
SQL warehouses, acesso a modelos de GenAI (Unity Gateway) e a associação dos grupos às serverless usage
policies — com a tag de custo `budget` em tudo o que é criado.

| Arquivo | Função |
|---|---|
| `onboarding_grupos.py` | Notebook principal, uma célula por etapa: 0 parâmetros e validação · 1 grupos e usuários · 2 schema e permissões · 3 SQL warehouse · 4 GenAI · 5 serverless usage policy · 6 resumo |
| `zerar_system_ai.py` | Rate limit 0 por usuário em todos os services de `system.ai` + liga `enforce_ai_gateway_v3_only` (modos `simular`, `aplicar`, `remover_zero`) |
| `onboarding_lib.py` | Funções idempotentes usadas pelos dois notebooks |
| `grupos_exemplo.csv` | Modelo do CSV (e-mails `@example.com`) |
| `deploy.sh <perfil> [pasta]` | Publica na pasta pessoal de quem faz o deploy e cria/atualiza o job diário `zerar_system_ai_diario` |
| `run_tests.sh [Classe[.teste]]` | Testes locais, sem workspace |

## Identidade
Os notebooks rodam com a autenticação de quem os executa (o admin do ambiente). Não há service principal.
Por isso ficam na **pasta pessoal do admin** (`deploy.sh` publica em `/Workspace/Users/<quem faz o deploy>/experimentacao_onboarding`):
se ficassem em `Shared`, qualquer usuário poderia editar o código que roda com privilégio de admin.
Os notebooks **param** se a própria pasta estiver acessível aos grupos `users`/`account users`.

Quem executa: workspace admin, com `USE_CATALOG`/`CREATE_SCHEMA` no catálogo e `SELECT` em `system.access.audit`;
para o `zerar_system_ai`, também metastore admin.

## CSV
```
grupo,emails,schema,warehouse,modelos,usage_policy,budget,tpm,qpm
fin_users,pessoa1@example.com;pessoa2@example.com,financas,wh_financas,gpt-5-mini;gte-large-en,exp_financas,foundationlab,,
```
| Coluna | Regra |
|---|---|
| `grupo`, `schema` | Obrigatórias; minúsculas, números e `_` |
| `emails`, `modelos` | Separados por `;`. `modelos` = nome do service em `system.ai`; vazio = grupo sem GenAI |
| `warehouse` | Vazio = warehouse padrão do notebook |
| `usage_policy` | **Nome** da serverless usage policy criada na interface; vazio = pula a etapa 5 |
| `budget` | Valor da tag de custo `budget`; vazio = nome do grupo. Use o mesmo valor na tag `budget` da usage policy |
| `tpm`, `qpm` | Limites do grupo por modelo; **vazio ou 0 = sem limite** |

## Uso
1. Para cada área, crie a serverless usage policy na interface (Settings > Compute > Serverless usage policies)
   com a tag `budget=<área>`.
2. Suba o CSV na mesma pasta dos notebooks (`.../experimentacao_onboarding/grupos.csv`).
3. Abra `onboarding_grupos`, rode a célula 0 e informe CSV (padrão `grupos.csv`), modo, catálogo (já existente)
   e grupo admin. Rode tudo em `simular`, confira, e depois em `aplicar`. Cada célula pode ser executada sozinha.
4. Uma vez: `zerar_system_ai` em `aplicar` com `conceder_manage = sim`. Depois o job diário mantém.

Reexecuções não duplicam nada: o que existe é reaproveitado, o que mudou no CSV é atualizado, o que é novo é criado.
Nada é removido — itens retirados do CSV continuam existindo (remoção manual).

A etapa 5 encontra a policy pelo nome em `system.access.audit`; policies recém-criadas podem levar algumas horas
para aparecer (a etapa mostra ALERTA — rode-a de novo mais tarde).

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
