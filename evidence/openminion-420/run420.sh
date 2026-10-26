#!/bin/bash
set -u
DEV="8c0568f70bd54190411da9926861543f166ebcb4"
F="tests/runtime/test_cron_capture_recovery.py tests/runtime/test_cron_turn_executor.py tests/memory/runtime/test_capture_recovery.py tests/memory/test_external_backend_capture.py tests/services/agent/test_execution_resources_memory.py"
RM="tests/runtime tests/memory/runtime tests/routines tests/services/runtime"
RMARK="not benchmark and not e2e and not postgres and not memory_eval_benchmark and not mcp_live and not package_integration and not telegram_live and not slack_live and not slow"
PROBE="/tmp/ev/probe_cron_capture_recovery.py"
PROBE2="/tmp/ev/probe_cron_recovery_turn.py"
P=.venv/bin/python; DEV=${DEV:-a41487ab1b51f7fd526ff29cd526f0b98dc01fc7}
sec() { echo; echo "== $*"; }
echo "head: $(git log --oneline -1)"; echo "dev:  $(git log --oneline -1 $DEV)"
echo "$($P -c 'import sys,sqlite3;print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'); $(grep PRETTY_NAME /etc/os-release)"
echo "packages: $($P -m pip list 2>/dev/null | grep -iE '^(sqlalchemy|psycopg|sophiagraph|typer) ' | tr -s ' ' | tr '\n' ';')"
echo "OPENMINION_TEST_POSTGRES_URL set: $([ -n "${OPENMINION_TEST_POSTGRES_URL:-}" ] && echo yes || echo no)"
mkdir -p /tmp/ev
cat > /tmp/ev/probe_cron_capture_recovery.py <<'PYEOF_X'
"""Consolidation cron run with pending capture intents: does recovery run?"""
import json, logging, os, sys, tempfile, traceback
from pathlib import Path
from types import SimpleNamespace
from openminion import Agent, APIRuntime
from openminion.base.config import ConfigManager, OpenMinionConfig, save_config
from openminion.modules.memory.backends.external import register_reference_sqlite_backend
from openminion.services.runtime.cron.executor import CronTurnExecutor
from tests._csc_fixtures import _csc_install_default_agent

logging.disable(logging.CRITICAL)
register_reference_sqlite_backend()
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    tmp = Path(tmp)
    os.environ["OPENMINION_HOME"] = str(tmp / "home")
    os.environ["OPENMINION_DATA_ROOT"] = str(tmp / "home" / "data")
    config = OpenMinionConfig()
    _csc_install_default_agent(config, provider="echo")
    config.runtime.process_mode = "single-process"
    config.runtime.daemon_auto_start = False
    config.storage.path = str(tmp / ".openminion" / "state.db")
    save_config(config, str(tmp / "agents.json"))
    manager = ConfigManager.load(str(tmp / "agents.json"), home_root=tmp, data_root=tmp / ".openminion")
    mem = {"store": {"backend": "sqlite", "sqlite_path": str(tmp / "m.db")},
           "backend": {"provider": "external", "external_adapter": "reference-sqlite", "options": {"db_path": str(tmp / "e.sqlite3")}}}
    manager.register("memory", lambda **kw: mem)
    runtime = APIRuntime.from_manager(manager)
    try:
        with Agent(runtime=runtime) as agent:
            try:
                r = agent.run("hello", session_id="cap"); print("turn:", r.run_state)
            except Exception as exc:
                print("turn raised:", type(exc).__name__)
        print("runtime_manager:", type(getattr(runtime, "runtime_manager", None)).__name__)
        print("has list_registered_agents:", hasattr(runtime, "list_registered_agents"))
        agent_id = next(e.payload["agent_id"] for s in runtime.sessions.list_sessions(limit=10) for e in runtime.sessions.list_events(session_id=s.id, limit=100) if e.event_type == "turn.outcome")
        print("agent_id:", agent_id)
        executor = CronTurnExecutor(runtime=runtime, cron_store=SimpleNamespace(), request_builder=lambda payload, agent: None, timeout_s=30)
        job = {"id": "job-1", "agent_id": agent_id, "payload": {"kind": "agentTurn", "message": "consolidate", "_openminion_memory_consolidation": {"target_scope": f"agent:{agent_id}"}}}
        try:
            out = executor.execute(job, {"id": "run-1"})
            print("execute returned:", json.dumps(out, default=str)[:400])
        except Exception as exc:
            tb = [f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in traceback.extract_tb(exc.__traceback__)][-5:]
            print("execute raised:", type(exc).__name__, str(exc)[:200]); print("  frames:", tb)
        # The same recovery call with the extractor's arguments passed by keyword.
        service = runtime.resolve_agent_service(agent_id)
        def by_keyword(session_api, session_id, root_turn_id, user_message):
            return service.extract_memory_capture_candidates(
                session_api=session_api, session_id=session_id,
                root_turn_id=root_turn_id, user_message=user_message)
        try:
            result = runtime.resolve_memory_assembly(agent_id).recover_pending_captures(
                sessions=runtime.sessions, agent_id=agent_id,
                extract_candidates=by_keyword, authorize=lambda *_: True)
            print("recovery with keyword arguments returned:", result)
        except Exception as exc:
            tb = [f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in traceback.extract_tb(exc.__traceback__)][-4:]
            print("recovery with keyword arguments raised:", type(exc).__name__, str(exc)[:200]); print("  frames:", tb)
    finally:
        runtime.close()
PYEOF_X
cat > /tmp/ev/probe_cron_recovery_turn.py <<'PYEOF_X'
"""Consolidation cron run with pending capture intents: does recovery run, and does the turn?

The #420 probe with two inputs changed so that the run can reach its scheduled
turn: the daemon's turn request builder (`build_turn_request`) instead of
`lambda payload, agent: None`, and a run dict shaped like the scheduler's
(`run_id`, `due_at`) instead of `{"id": ...}`. It also records the recovery result.
"""
import json, logging, os, sys, tempfile, traceback
from pathlib import Path
from types import SimpleNamespace
from openminion import Agent, APIRuntime
from openminion.base.config import ConfigManager, OpenMinionConfig, save_config
from openminion.modules.memory.backends.external import register_reference_sqlite_backend
from openminion.services.runtime.cron.executor import CronTurnExecutor
from openminion.services.runtime.daemon import build_turn_request
from tests._csc_fixtures import _csc_install_default_agent

logging.disable(logging.CRITICAL)
register_reference_sqlite_backend()
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    tmp = Path(tmp)
    os.environ["OPENMINION_HOME"] = str(tmp / "home")
    os.environ["OPENMINION_DATA_ROOT"] = str(tmp / "home" / "data")
    config = OpenMinionConfig()
    _csc_install_default_agent(config, provider="echo")
    config.runtime.process_mode = "single-process"
    config.runtime.daemon_auto_start = False
    config.storage.path = str(tmp / ".openminion" / "state.db")
    save_config(config, str(tmp / "agents.json"))
    manager = ConfigManager.load(str(tmp / "agents.json"), home_root=tmp, data_root=tmp / ".openminion")
    mem = {"store": {"backend": "sqlite", "sqlite_path": str(tmp / "m.db")},
           "backend": {"provider": "external", "external_adapter": "reference-sqlite", "options": {"db_path": str(tmp / "e.sqlite3")}}}
    manager.register("memory", lambda **kw: mem)
    runtime = APIRuntime.from_manager(manager)
    try:
        with Agent(runtime=runtime) as agent:
            try:
                r = agent.run("hello", session_id="cap"); print("turn:", r.run_state)
            except Exception as exc:
                print("turn raised:", type(exc).__name__)
        print("runtime_manager:", type(getattr(runtime, "runtime_manager", None)).__name__)
        print("has list_registered_agents:", hasattr(runtime, "list_registered_agents"))
        agent_id = next(e.payload["agent_id"] for s in runtime.sessions.list_sessions(limit=10) for e in runtime.sessions.list_events(session_id=s.id, limit=100) if e.event_type == "turn.outcome")
        print("agent_id:", agent_id)
        executor = CronTurnExecutor(runtime=runtime, cron_store=SimpleNamespace(), request_builder=lambda payload, agent: build_turn_request(payload, default_agent_id=agent), timeout_s=30)
        job = {"id": "job-1", "agent_id": agent_id, "payload": {"kind": "agentTurn", "message": "consolidate", "_openminion_memory_consolidation": {"target_scope": f"agent:{agent_id}"}}}
        try:
            assembly = runtime.resolve_memory_assembly(agent_id); real = assembly.recover_pending_captures
            assembly.recover_pending_captures = lambda **kw: print("recovery result:", real(**kw))
            out = executor.execute(job, {"run_id": "run-1", "due_at": "2026-10-26T00:00:00+00:00"})
            print("execute returned: summary", repr(out.get("summary")), "isolated_session_id", out.get("isolated_session_id"), "watermark scope", out.get("output", {}).get("coordination_watermark", {}).get("target_scope"))
        except Exception as exc:
            tb = [f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in traceback.extract_tb(exc.__traceback__)][-5:]
            print("execute raised:", type(exc).__name__, str(exc)[:200]); print("  frames:", tb)
        # The same recovery call with the extractor's arguments passed by keyword.
        service = runtime.resolve_agent_service(agent_id)
        def by_keyword(session_api, session_id, root_turn_id, user_message):
            return service.extract_memory_capture_candidates(
                session_api=session_api, session_id=session_id,
                root_turn_id=root_turn_id, user_message=user_message)
        try:
            result = runtime.resolve_memory_assembly(agent_id).recover_pending_captures(
                sessions=runtime.sessions, agent_id=agent_id,
                extract_candidates=by_keyword, authorize=lambda *_: True)
            print("recovery with keyword arguments returned:", result)
        except Exception as exc:
            tb = [f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in traceback.extract_tb(exc.__traceback__)][-4:]
            print("recovery with keyword arguments raised:", type(exc).__name__, str(exc)[:200]); print("  frames:", tb)
    finally:
        runtime.close()
PYEOF_X
[ -n "${PROBE:-}" ] && sha256sum /tmp/ev/*
pyt() { $P -m pytest -q -p no:cacheprovider -rfE --tb=line "$@" 2>&1 | grep -E "^FAILED|^ERROR|passed|failed|error" | sed -E 's/^(.{240}).*/\1 .../'; }
for tree in head dev; do
  if [ $tree = dev ]; then git checkout -q $DEV -- src; sec "dev sources from here on: git checkout $DEV -- src ($(git status --short | grep -v '^?? .deps' | wc -l) paths differ)"; fi
  sec "[$tree] pytest <focused>"; pyt $F
  if [ -n "${R:-}" ]; then sec "[$tree] pytest <related>"; pyt $R; fi
  if [ -n "${RM:-}" ]; then sec "[$tree] pytest -m \"$RMARK\" $RM"; pyt -m "$RMARK" $RM; fi
  if [ -n "${PGX:-}" ]; then sec "[$tree] pytest -m postgres <postgres>"; pyt -m postgres $PGX; fi
  if [ -n "${PROBE:-}" ]; then sec "[$tree] the issue's probe: PYTHONPATH=src:. python $(basename $PROBE)"; PYTHONPATH=src:. $P $PROBE 2>&1 | grep -v "^20[0-9][0-9]-"; fi
  if [ -n "${PROBE2:-}" ]; then sec "[$tree] PYTHONPATH=src:. python $(basename $PROBE2)"; PYTHONPATH=src:. $P $PROBE2 2>&1 | grep -v "^20[0-9][0-9]-"; fi
done
git checkout -q HEAD -- src; sec "back to the branch head: $(git status --short | grep -v '^?? .deps' | wc -l) paths differ"
sec "ruff check ."; .venv/bin/ruff check . 2>&1 | tail -3
sec "make format-check"; make format-check 2>&1 | tail -2
sec "make workflow-check"; make workflow-check 2>&1 | tail -4
sec "make lint"; make lint 2>&1 | tail -40; echo "make lint exit: ${PIPESTATUS[0]}"
sec "make test-critical"; make test-critical 2>&1 | tail -3
