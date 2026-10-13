#!/bin/sh
# PostgreSQL 17 and its PostGIS 3.5.2 extension must already be installed.
set -eu
cd "$(dirname "$0")/.."
runtime="$PWD/output/runtime/native-pg17-v004"
pgbin="${UBEM_PG_BIN:-/opt/homebrew/opt/postgresql@17/bin}"
mkdir -p "$runtime"
created=false
if [ ! -f "$runtime/data/PG_VERSION" ]; then
    "$pgbin/initdb" -D "$runtime/data" -U ubem_owner --locale=C.UTF-8 --encoding=UTF8 --auth-local=trust --auth-host=scram-sha-256
    cat config/native-postgresql17.conf >> "$runtime/data/postgresql.conf"
    created=true
fi
[ "$(cat "$runtime/data/PG_VERSION")" = 17 ]
if ! "$pgbin/pg_ctl" -D "$runtime/data" status; then
    "$pgbin/pg_ctl" -D "$runtime/data" -l "$runtime/server.log" start
fi
"$pgbin/psql" -h /tmp -p 55433 -U ubem_owner -d postgres -c "SELECT version()"
if [ "$created" = true ]; then
    "$pgbin/psql" -h /tmp -p 55433 -U ubem_owner -d postgres -c "ALTER ROLE ubem_owner PASSWORD 'ubem-local-only'"
    "$pgbin/createdb" -h /tmp -p 55433 -U ubem_owner sweden_ubem_v004
    "$pgbin/psql" -h /tmp -p 55433 -U ubem_owner -d sweden_ubem_v004 -c "CREATE EXTENSION postgis"
fi
