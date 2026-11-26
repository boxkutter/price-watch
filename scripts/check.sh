#!/usr/bin/env bash
# Local replacement for CI. Lint, the test suite on SQLite, then (unless --fast) the suite again
# plus the migrations on PostgreSQL 17 in a throwaway Docker container that is always removed.
#   scripts/check.sh          everything
#   scripts/check.sh --fast   lint + SQLite only (no Docker)
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== lint";           uv run --no-sync ruff check app tests migrations examples connectors
echo "== tests (SQLite)"; uv run --no-sync pytest -q
[ "${1:-}" = "--fast" ] && { echo "OK (fast)"; exit 0; }

name="pw-check-$$"; port=$((55000 + RANDOM % 1000))
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker run -d --rm --name "$name" -e POSTGRES_USER=retail -e POSTGRES_PASSWORD=check \
  -e POSTGRES_DB=retail -p "127.0.0.1:$port:5432" postgres:17 >/dev/null
# PostgreSQL restarts once during first boot, so wait for a real query, not just pg_isready.
sleep 5
until docker exec "$name" psql -U retail -d retail -c 'select 1' >/dev/null 2>&1; do sleep 1; done
base="postgresql+psycopg://retail:check@127.0.0.1:$port"

echo "== tests (PostgreSQL)"
TEST_DATABASE_URL="$base/retail" uv run --no-sync pytest -q
echo "== migrations (PostgreSQL)"
docker exec "$name" psql -U retail -d retail -c 'create database migrated' >/dev/null
DATABASE_URL="$base/migrated" uv run --no-sync alembic upgrade head
DATABASE_URL="$base/migrated" uv run --no-sync alembic check
echo "OK"
