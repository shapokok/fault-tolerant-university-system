#!/bin/bash
# Runs inside the backup container: pg_dump every BACKUP_INTERVAL_S seconds,
# keeps the newest BACKUP_KEEP files. Falls back to the replica if the primary is down.
while true; do
  f="/backups/uni_$(date +%Y%m%d_%H%M%S).dump"
  for host in postgres-primary postgres-replica; do
    if pg_dump -h "$host" -U uni -Fc uni > "$f.tmp" 2>/dev/null; then
      mv "$f.tmp" "$f"; echo "backup ok from $host: $f"; break
    fi
  done
  rm -f "$f.tmp"
  ls -1t /backups/*.dump 2>/dev/null | tail -n +$((BACKUP_KEEP + 1)) | xargs -r rm
  sleep "$BACKUP_INTERVAL_S"
done
