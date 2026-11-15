#!/bin/bash
# Runs once, when the Postgres volume is first created.
# One database and one login role per service. A role cannot connect to
# another service's database.
set -euo pipefail

for svc in auth bank card orchestrator; do
  password_var="$(echo "$svc" | tr '[:lower:]' '[:upper:]')_DB_PASSWORD"
  # The password goes in as a psql variable so that psql quotes it.
  psql -v ON_ERROR_STOP=1 -v password="${!password_var}" --username "$POSTGRES_USER" <<SQL
CREATE ROLE ${svc} LOGIN PASSWORD :'password';
CREATE DATABASE ${svc} OWNER ${svc};
REVOKE ALL ON DATABASE ${svc} FROM PUBLIC;
SQL
done
