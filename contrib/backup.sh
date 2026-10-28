#!/usr/bin/env bash
# Backup Postgres + the blob volume. Usage: contrib/backup.sh [outdir]   (restore: see README "Backup & restore")
set -euo pipefail
out="${1:-./backups}/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$out"
docker compose exec -T postgres pg_dump -U mizan mizan | gzip > "$out/db.sql.gz"
docker compose run --rm --no-deps -v "$PWD/$out":/backup api sh -c 'tar czf /backup/blobs.tgz -C /data .'
echo "backup written to $out"
