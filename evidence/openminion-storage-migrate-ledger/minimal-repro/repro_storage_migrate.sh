#!/usr/bin/env bash
# The minimal reproduction from the storage-migrate-ledger report, command for command.
# Needs `openminion` and `python` of the venv under test first on PATH, and PGURL
# (a PostgreSQL server URL). Passwords are masked in the output.
set -uo pipefail
mask() { sed -E 's#(postgresql(\+[a-z]+)?://[^:/@ ]+):[^@ ]*@#\1:***@#g'; }

newdb() {  # prints the URL of a new, empty database
  python - "$1" <<'EOF'
import os, sys, sqlalchemy as sa
from urllib.parse import urlsplit, urlunsplit
base = urlsplit(os.environ["PGURL"])
admin = sa.create_engine(urlunsplit(base._replace(path="/postgres")), isolation_level="AUTOCOMMIT")
with admin.connect() as c:
    c.execute(sa.text(f'DROP DATABASE IF EXISTS "{sys.argv[1]}" WITH (FORCE)'))
    c.execute(sa.text(f'CREATE DATABASE "{sys.argv[1]}"'))
admin.dispose()
print(urlunsplit(base._replace(path="/" + sys.argv[1])))
EOF
}

facts() {  # tables, alembic_version and om_meta.module_id of a database
  python - "$1" <<'EOF'
import sys, sqlalchemy as sa
e = sa.create_engine(sys.argv[1])
with e.connect() as c:
    t = c.execute(sa.text("SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY 1")).scalars().all()
    av = c.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all() if "alembic_version" in t else None
    om = c.execute(sa.text("SELECT value FROM om_meta WHERE key='module_id'")).scalar() if "om_meta" in t else None
e.dispose()
print(f"  facts: {len(t)} tables; registry tables present: {[x for x in ('agents','agent_methods','agent_status') if x in t]}; alembic_version={av}; om_meta.module_id={om}")
EOF
}

run() { echo "\$ $*" | mask; "$@" 2>/dev/null | mask; echo "  exit=${PIPESTATUS[0]}"; }

if [ "${1:-}" = "--newdb" ]; then newdb "$2"; exit; fi
LABEL="${1:-x}"
echo "### 1. sessions"
URL=$(newdb "repro_${LABEL}_1")
run openminion storage migrate --backend postgres --postgres-url "$URL" --module session --json
echo "\$ python - \"\$URL\" <<'EOF' ... build_runtime_storage ... EOF"
TMPF=$(mktemp)
python - "$URL" > "$TMPF" 2>&1 <<'EOF'
import sys
from openminion.base.config.parser import openminion_config_from_dict
from openminion.modules.storage.runtime.context import build_runtime_storage
config = openminion_config_from_dict({
    "default_agent": "first",
    "agents": {"first": {"provider": "echo", "default_channel": "console"}},
    "storage": {"path": "state.db", "backend": "postgres", "postgres_url": sys.argv[1]},
})
build_runtime_storage(config.storage.path, env={},
                      record_backend=config.storage.record_backend(),
                      record_backend_options=config.storage.record_backend_options())
EOF
echo "  exit=$?"
grep -E "^[a-zA-Z_][a-zA-Z0-9_.]*: " "$TMPF" | mask
rm -f "$TMPF"

echo "### 2a. session, then artifact"
URL=$(newdb "repro_${LABEL}_2a")
run openminion storage migrate --backend postgres --postgres-url "$URL" --module session --json
run openminion storage migrate --backend postgres --postgres-url "$URL" --module artifact --json
facts "$URL"

echo "### 2b. artifact, then registry"
URL2=$(newdb "repro_${LABEL}_2b")
run openminion storage migrate --backend postgres --postgres-url "$URL2" --module artifact --json
facts "$URL2"
run openminion storage migrate --backend postgres --postgres-url "$URL2" --module registry --json
facts "$URL2"
