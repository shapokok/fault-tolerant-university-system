#!/bin/bash
# Restore a backup into the primary (the replica receives it through replication).
# Usage: infra/restore.sh [backups/uni_XXXX.dump]   (default: newest backup)
set -e
FILE=${1:-$(ls -1t backups/*.dump | head -1)}
echo "restoring $FILE"
docker compose -f docker-compose.ft.yml exec -T postgres-primary \
  pg_restore -U uni -d uni --clean --if-exists < "$FILE"
echo "done"
