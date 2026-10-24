"""External memory provider: can an SDK turn run?

An SDK runtime (echo provider, explicit roots) runs two turns in one session,
once with the default memory provider (control) and once with
`memory.backend.provider: external` and the bundled `reference-sqlite` adapter.
For each turn it reports the run state, or the exception `Agent.run` raised and
the openminion frames of its traceback (innermost last). For the external run it
also reports what the capture path sees: the store behind the memory service,
whether that store has a callable `apply_capture_bundle`, whether the adapter's
backend has one, and whether the raised exception is one of the types the
terminal capture step handles. Last, it counts the capture intents the turns
committed and the capture results recorded for them.

Run from an openminion source checkout: PYTHONPATH="src;." python probe_external_capture.py
(PYTHONPATH=src:. on Linux/macOS)
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

import openminion
from openminion import Agent, APIRuntime
from openminion.base.config import ConfigManager, OpenMinionConfig, save_config
from openminion.modules.memory.backends.external import (
    register_reference_sqlite_backend,
)
from openminion.modules.memory.errors import (
    CaptureBundleIntegrityError,
    CaptureRecoveryUnsupportedError,
    MemctlError,
)
from tests._csc_fixtures import _csc_install_default_agent

# The exception types `_commit_terminal_capture` (brain/runner/coordinator.py)
# catches around `apply_capture_bundle`.
HANDLED = (CaptureBundleIntegrityError, CaptureRecoveryUnsupportedError, MemctlError)
SRC = Path(openminion.__file__).resolve().parent


def frames(exc):
    """The outermost openminion frame, then every frame from the brain runner in."""
    out = []
    for frame in traceback.extract_tb(exc.__traceback__):
        path = Path(frame.filename).resolve()
        if SRC in path.parents:
            out.append(f"{path.relative_to(SRC.parent).as_posix()}:{frame.lineno} {frame.name}")
    runner = next((i for i, f in enumerate(out) if "/brain/runner/" in f), len(out))
    return out[:1] + (["..."] if runner > 1 else []) + out[runner:]


def run_case(root, memory_config):
    config = OpenMinionConfig()
    _csc_install_default_agent(config, provider="echo")
    config.runtime.process_mode = "single-process"
    config.runtime.daemon_auto_start = False
    config.storage.path = str(root / ".openminion" / "state.db")
    config_path = root / "agents.json"
    save_config(config, str(config_path))
    manager = ConfigManager.load(str(config_path), home_root=root, data_root=root / ".openminion")
    if memory_config is not None:
        manager.register("memory", lambda **kwargs: memory_config)
    runtime = APIRuntime.from_manager(manager)
    row = {}
    try:
        [assembly] = list(runtime._memory_assemblies.values())
        service_store = assembly.service._store
        row["memory service store"] = type(service_store).__name__
        inner = getattr(service_store, "_store", None)
        if inner is not None:
            row["memory service store wraps"] = type(inner).__name__
        target = inner if inner is not None else service_store
        backend = getattr(target, "_backend", None)
        if backend is not None:
            row["adapter backend"] = type(backend).__name__
            row["backend has apply_capture_bundle"] = callable(
                getattr(backend, "apply_capture_bundle", None)
            )
        row["service store has callable apply_capture_bundle"] = callable(
            getattr(service_store, "apply_capture_bundle", None)
        )
        turns = []
        with Agent(runtime=runtime) as agent:
            for _ in range(2):
                try:
                    result = agent.run("hello", session_id="capture-probe")
                    turns.append({"run_state": result.run_state})
                except Exception as exc:  # noqa: BLE001
                    turns.append(
                        {
                            "raised": f"{type(exc).__name__}: {exc}",
                            "handled by the capture step": isinstance(exc, HANDLED),
                            "openminion frames": frames(exc),
                        }
                    )
        row["turns"] = turns
        sessions = runtime.sessions
        outcomes, results = 0, 0
        for session in sessions.list_sessions(limit=100):
            for event in sessions.list_events(session_id=session.id, limit=1000):
                if event.event_type == "turn.outcome":
                    outcomes += event.payload.get("capture_state") == "pending"
                results += event.event_type == "memory.capture.result"
        row["capture intents (turn.outcome, capture_state pending)"] = outcomes
        row["capture results (memory.capture.result)"] = results
        return row
    finally:
        runtime.close()


def checkout():
    root = SRC.parents[1]
    sha = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short=8", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    return f"checkout {sha}" if sha else "installed package"


def main():
    logging.disable(logging.CRITICAL)
    register_reference_sqlite_backend()
    rows = {}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        os.environ["OPENMINION_HOME"] = str(tmp / "home")
        os.environ["OPENMINION_DATA_ROOT"] = str(tmp / "home" / "data")
        rows["default provider (control)"] = run_case(tmp / "a", None)
        rows["external provider, reference-sqlite adapter"] = run_case(
            tmp / "b",
            {
                "store": {"backend": "sqlite", "sqlite_path": str(tmp / "b-memory.db")},
                "backend": {
                    "provider": "external",
                    "external_adapter": "reference-sqlite",
                    "options": {"db_path": str(tmp / "b-external.sqlite3")},
                },
            },
        )
    print(json.dumps(rows, indent=2))
    print("openminion", openminion.__version__, "from", checkout(), "| python", sys.version.split()[0], sys.platform)


if __name__ == "__main__":
    main()
