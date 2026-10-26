# Onboarding de grupos — ambiente de experimentação

Notebooks para cadastrar grupos, usuários, schemas, warehouses e acesso a modelos (Unity Gateway).

| Arquivo | Função |
|---|---|
| `onboarding_grupos.py` | Lê o CSV e aplica, por grupo: grupo de account + usuários + atribuição ao workspace, schema com grants fixos, SQL warehouse serverless, model services com TPM/QPM |
| `zerar_system_ai.py` | Rate limit 0 por usuário em todos os services de `system.ai` (modos `simular`, `aplicar`, `remover_zero`) |
| `onboarding_lib.py` | Funções idempotentes usadas pelos dois notebooks |
| `grupos_exemplo.csv` | Modelo do CSV (e-mails `@example.com`) |
| `deploy.sh <perfil> [pasta]` | Publica na pasta pessoal de quem faz o deploy e cria/atualiza o job diário `zerar_system_ai_diario` |
| `run_tests.sh [Classe[.teste]]` | Testes locais, sem workspace |

## CSV
```
grupo,emails,schema,warehouse,modelos,tpm,qpm
fin_users,pessoa1@example.com;pessoa2@example.com,financas,wh_financas,gpt-5-mini;gte-large-en,50000,60
```
`emails` e `modelos` separados por `;`. `warehouse`, `tpm`, `qpm` vazios usam o padrão do notebook.
`modelos` = nome do service em `system.ai`.

## Identidade
Os notebooks rodam com a autenticação de quem os executa (o admin do ambiente). Não há service principal.
Por isso ficam na **pasta pessoal do admin** (`deploy.sh` publica em `/Workspace/Users/<quem faz o deploy>/experimentacao_onboarding`):
se ficassem em `Shared`, qualquer usuário poderia editar o código que roda com privilégio de admin.
Os notebooks **param** se a própria pasta estiver acessível aos grupos `users`/`account users`.
O admin precisa ser workspace admin, ter `USE_CATALOG`/`CREATE_SCHEMA` no catálogo e, para o `zerar_system_ai`, ser metastore admin.

## Uso
1. Suba o CSV na mesma pasta dos notebooks (`/Workspace/Users/<admin>/experimentacao_onboarding/grupos.csv`).
2. O admin abre `onboarding_grupos`, informa o nome do CSV (padrão `grupos.csv`), catálogo (já existente) e grupo admin; roda em `simular`, confere, roda em `aplicar`.
3. Uma vez: `zerar_system_ai` em `aplicar` com `conceder_manage = sim` (precisa ser metastore admin). Depois o job diário mantém.

## Validação com um usuário de área (não admin)
Admins e owners passam por cima dos grants — a validação precisa ser feita por um usuário comum da área:
1. criar tabela no schema da própria área → funciona;
2. criar tabela no schema de outra área → negado;
3. `CREATE VOLUME` → negado;
4. usar o warehouse da área → funciona; o de outra área → negado;
5. chamar o model service da área → funciona; `system.ai` direto → bloqueado (após ~5 min do zero).

## Limitações conhecidas
- `ai_query` e os endpoints legados `/serving-endpoints/databricks-*` não respeitam o zero de `system.ai`; `ai_query` não aceita os model services dos grupos. Controle desses caminhos: budget do Unity Gateway (account console).
- Mudanças de rate limit levam ~5 min para valer em todas as chamadas.
- Budget policy e budgets ficam fora (APIs só de account).
