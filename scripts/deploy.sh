#!/usr/bin/env bash
set -euo pipefail

: "${SERVER_HOST:?Set SERVER_HOST, for example: 1.2.3.4}"
: "${SERVER_USER:=root}"
: "${SERVER_PORT:=22}"
: "${REMOTE_DIR:=/opt/auto_cut_engine}"

LOCAL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ ! -f "$LOCAL_ROOT/.env.production" ]; then
  echo "Missing .env.production. Copy .env.production.example and fill production secrets first." >&2
  exit 1
fi

ssh -p "$SERVER_PORT" "$SERVER_USER@$SERVER_HOST" "mkdir -p '$REMOTE_DIR'"

rsync -az --delete \
  --exclude '.git' \
  --exclude '.env' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '.DS_Store' \
  --exclude 'media/uploads/*' \
  --exclude 'media/results/*' \
  --exclude 'media/creative/*' \
  --exclude 'media/previews/*' \
  --exclude 'media/freezes/*' \
  -e "ssh -p $SERVER_PORT" \
  "$LOCAL_ROOT/" "$SERVER_USER@$SERVER_HOST:$REMOTE_DIR/"

ssh -p "$SERVER_PORT" "$SERVER_USER@$SERVER_HOST" "cd '$REMOTE_DIR' && docker compose --env-file .env.production up -d --build --remove-orphans"
ssh -p "$SERVER_PORT" "$SERVER_USER@$SERVER_HOST" "cd '$REMOTE_DIR' && docker compose --env-file .env.production ps"
