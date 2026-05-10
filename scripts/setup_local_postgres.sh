#!/usr/bin/env bash
set -euo pipefail

DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-freight}"
DB_USER="${DB_USER:-freight}"
DB_PASSWORD="${DB_PASSWORD:-freight}"
ADMIN_DB="${ADMIN_DB:-postgres}"

if ! command -v psql >/dev/null 2>&1; then
  echo "psql was not found."
  echo "Install Postgres locally first, for example:"
  echo "  brew install postgresql@16"
  echo "  brew services start postgresql@16"
  exit 1
fi

if ! command -v pg_isready >/dev/null 2>&1; then
  echo "pg_isready was not found. Make sure PostgreSQL client tools are on PATH."
  exit 1
fi

if ! pg_isready -h "${DB_HOST}" -p "${DB_PORT}" >/dev/null 2>&1; then
  echo "Postgres is not accepting connections at ${DB_HOST}:${DB_PORT}."
  echo "Start local Postgres first, for example:"
  echo "  brew services start postgresql@16"
  exit 1
fi

echo "Creating/updating local Postgres role '${DB_USER}' and database '${DB_NAME}'..."

psql -h "${DB_HOST}" -p "${DB_PORT}" -d "${ADMIN_DB}" -v ON_ERROR_STOP=1 \
  -v db_user="${DB_USER}" \
  -v db_password="${DB_PASSWORD}" \
  -v db_name="${DB_NAME}" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'db_user', :'db_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'db_user')\gexec

SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'db_user', :'db_password')\gexec

SELECT format('CREATE DATABASE %I OWNER %I', :'db_name', :'db_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'db_name')\gexec

GRANT ALL PRIVILEGES ON DATABASE :"db_name" TO :"db_user";
SQL

echo "Local Postgres is ready."
echo "DATABASE_URL=postgresql+psycopg://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
