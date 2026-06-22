#!/usr/bin/env bash

set -euo pipefail

COMPOSE="${COMPOSE:-docker compose}"
RUN_POST_DEPLOY_CHECK="${RUN_POST_DEPLOY_CHECK:-1}"

printf 'Publishing StudyAgent frontend bundle through nginx image\n'
printf 'COMPOSE=%s\n' "$COMPOSE"

$COMPOSE build nginx
$COMPOSE up -d --no-deps nginx

if [[ "$RUN_POST_DEPLOY_CHECK" == "1" ]]; then
  bash scripts/post_deploy_check.sh
fi

printf 'Frontend bundle published.\n'
