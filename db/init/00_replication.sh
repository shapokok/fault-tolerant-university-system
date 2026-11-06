#!/bin/bash
# Lets the streaming replica connect. The password comes from .env (REPLICATOR_PASSWORD).
# The baseline has no replica and no such variable, so it creates no replication role.
set -e
if [ -z "$REPLICATOR_PASSWORD" ]; then exit 0; fi
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v pw="$REPLICATOR_PASSWORD" <<'SQL'
CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD :'pw';
SQL
echo "host replication replicator all scram-sha-256" >> "$PGDATA/pg_hba.conf"
