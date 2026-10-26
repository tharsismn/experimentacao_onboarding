#!/usr/bin/env bash
# Publica os notebooks e a lib na pasta PESSOAL de quem faz o deploy (só o admin acessa).
# Uso: ./deploy.sh <perfil> [pasta-destino]
set -euo pipefail
cd "$(dirname "$0")"
P=${1:?perfil do CLI}
ME=$(databricks current-user me -p "$P" -o json | jq -r .userName)
DEST=${2:-/Workspace/Users/$ME/experimentacao_onboarding}
databricks workspace mkdirs "$DEST" -p "$P"
databricks workspace import "$DEST/onboarding_lib.py" --file onboarding_lib.py --format AUTO --overwrite -p "$P"
for nb in onboarding_grupos zerar_system_ai; do
  databricks workspace import "$DEST/$nb" --file "$nb.py" --language PYTHON --format SOURCE --overwrite -p "$P"
done
echo "publicado em $DEST"

# Job diário que reaplica o zero em system.ai (modelos novos). Roda como quem executa o deploy.
JOB_NAME="zerar_system_ai_diario"
JOB_JSON=$(jq -n --arg nb "$DEST/zerar_system_ai" --arg n "$JOB_NAME" '{
  name: $n,
  schedule: {quartz_cron_expression: "0 0 6 * * ?", timezone_id: "America/Sao_Paulo", pause_status: "UNPAUSED"},
  max_concurrent_runs: 1,
  tasks: [{task_key: "zerar", notebook_task: {notebook_path: $nb, base_parameters: {modo: "aplicar", conceder_manage: "nao"}}}]}')
JOB_ID=$(databricks jobs list -p "$P" -o json --name "$JOB_NAME" | jq -r '.[0].job_id // empty')
if [[ -n "$JOB_ID" ]]; then
  databricks jobs reset -p "$P" --json "$(jq -n --argjson id "$JOB_ID" --argjson s "$JOB_JSON" '{job_id:$id, new_settings:$s}')"
  echo "job $JOB_NAME atualizado ($JOB_ID)"
else
  JOB_ID=$(databricks jobs create -p "$P" -o json --json "$JOB_JSON" | jq -r .job_id)
  echo "job $JOB_NAME criado ($JOB_ID)"
fi
