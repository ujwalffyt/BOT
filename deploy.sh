#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
command -v docker >/dev/null 2>&1 || { echo "Docker Engine is required." >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Docker Compose plugin is required." >&2; exit 1; }
mkdir -p data work caddy/sites
chmod 700 data
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env. Edit DISCORD_BOT_TOKEN, ADMIN_IDS and PUBLIC_HOST, then run ./deploy.sh again."
  exit 0
fi
docker compose up -d --build
echo "NeonHost Discord bot started. View logs with: docker compose logs -f bot"
