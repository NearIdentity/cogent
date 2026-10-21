#!/bin/sh
# Runs once, when the data volume is first created (docker-entrypoint-initdb.d): creates the SELECT-only role with a password from the environment.
set -e
: "${COGENT_RO_PASSWORD:?COGENT_RO_PASSWORD is not set}"
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ro_password="$COGENT_RO_PASSWORD" -f /tabledb-readonly-role.sql
