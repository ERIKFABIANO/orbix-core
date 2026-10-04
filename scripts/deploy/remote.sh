#!/usr/bin/env bash
# Roda NA DROPLET (chamado por deploy.ps1): instala o ambiente, carrega a imagem e sobe os containers.
set -euo pipefail
cd /opt/orbix
python3 incoming/merge_env.py
docker load -q -i incoming/orbix-api.tar
find incoming -type f -delete
rmdir incoming

docker compose --env-file compose.env up -d --remove-orphans 2>&1 | tail -8
# o Caddyfile é montado como arquivo: o Caddy só relê ao reiniciar
docker compose --env-file compose.env restart caddy >/dev/null
sleep 20
echo "--- status"
docker compose --env-file compose.env ps --format 'table {{.Service}}\t{{.Status}}'
echo "--- memória"
docker stats --no-stream --format '{{.Name}} {{.MemUsage}}'
docker image prune -af --filter "until=1h" >/dev/null 2>&1 || true
df -h / | tail -1
