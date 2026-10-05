#!/bin/bash
# Promote the replica to primary. Use when postgres-primary is dead.
# Services try the primary first and then the replica, so after promotion
# writes succeed on the replica without restarting anything.
# Do not start the old primary again afterwards (it would be a second primary).
set -e
COMPOSE="docker compose -f docker-compose.ft.yml"
$COMPOSE exec -T postgres-replica psql -U uni -d uni -tAc "SELECT pg_promote();"
echo -n "replica still in recovery (f = it is now the primary): "
$COMPOSE exec -T postgres-replica psql -U uni -d uni -tAc "SELECT pg_is_in_recovery();"
