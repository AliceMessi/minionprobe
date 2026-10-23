"""Reproduction for openminion/openminion#369: session metadata filters on SQLite vs PostgreSQL.

Run from an OpenMinion source checkout (with the `postgres` extra installed) and
OPENMINION_TEST_POSTGRES_URL pointing at a disposable PostgreSQL 16 server:

    python repro_369.py

It creates one runtime store per backend, a session with metadata {"surface": "acp"},
then calls list_sessions with a matching filter, an unmatched filter and no filter.
"""

import json
import os
import tempfile
import traceback
import uuid
from pathlib import Path
from urllib.parse import quote_plus

import openminion
from openminion.modules.storage.runtime.context import build_runtime_storage


def _schema_url(base_url: str, schema_name: str) -> str:
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}options={quote_plus(f'-csearch_path={schema_name}')}"


def probe(label: str, storage) -> dict:
    sessions = storage.sessions
    session = sessions.resolve_session(
        agent_id="repro-agent", channel="acp", target="repro-369", metadata={"surface": "acp"}
    )
    result = {"backend": label, "session_metadata": dict(session.metadata)}
    for name, metadata_filter in (
        ("matching", {"surface": "acp"}),
        ("unmatched", {"surface": "focus"}),
        ("none", None),
    ):
        try:
            rows = sessions.list_sessions(metadata_filter=metadata_filter)
            result[name] = {"ok": True, "ids": [row.id == session.id for row in rows]}
        except Exception as exc:  # noqa: BLE001 - the error is the observation
            result[name] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}",
                "trace_tail": traceback.format_exc().splitlines()[-3:],
            }
    return result


def main() -> None:
    print(json.dumps({"openminion": openminion.__file__.split("src")[-1], "version": getattr(openminion, "__version__", "?")}))
    with tempfile.TemporaryDirectory() as tmp:
        sqlite_storage = build_runtime_storage(Path(tmp) / "sqlite" / "openminion.db", env={})
        print(json.dumps(probe("sqlite", sqlite_storage), indent=2))

        postgres_url = os.environ["OPENMINION_TEST_POSTGRES_URL"]
        import sqlalchemy

        schema_name = f"repro369_{uuid.uuid4().hex[:12]}"
        admin = sqlalchemy.create_engine(postgres_url, future=True)
        with admin.begin() as conn:
            conn.execute(sqlalchemy.text(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"'))
        postgres_storage = build_runtime_storage(
            Path(tmp) / "pg" / "openminion.db",
            env={},
            record_backend="record.postgres",
            record_backend_options={"url": _schema_url(postgres_url, schema_name)},
        )
        print(json.dumps(probe("postgresql", postgres_storage), indent=2))
        with admin.connect() as conn:
            print(json.dumps({"server_version": conn.execute(sqlalchemy.text("SHOW server_version")).scalar()}))


if __name__ == "__main__":
    main()
