#!/bin/bash
# First start: copy the primary with pg_basebackup. -R writes standby settings,
# so this server starts as a read-only streaming replica.
set -e
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  until PGPASSWORD=replicator pg_basebackup -h postgres-primary -U replicator \
        -D "$PGDATA" -R -X stream; do
    echo "waiting for primary..."; sleep 1
  done
fi
exec docker-entrypoint.sh postgres
