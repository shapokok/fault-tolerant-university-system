#!/bin/bash
# Lets the streaming replica connect (harmless in baseline, where no replica exists).
set -e
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
     -c "CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD 'replicator';"
echo "host replication replicator all scram-sha-256" >> "$PGDATA/pg_hba.conf"
