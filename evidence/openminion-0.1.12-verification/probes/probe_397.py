"""#397 on the installed package: SDK turns with an external backend that lacks atomic capture.

Each case runs two real `Agent.run` echo turns in one session through an SDK
runtime whose memory config selects `memory.backend.provider: external`, then
reads the session's capture events, runs one pending-capture recovery pass
(installed `recover_pending_captures`; the extractor is not under test) and
closes the runtime three times.

  A. Bundled `reference-sqlite` adapter (no apply_capture_bundle): both turns
     complete; each capture stays pending with CAPTURE_RECOVERY_UNSUPPORTED in
     memory_capture_bundle_result and no receipt fields; no memory.capture.result
     event; recovery scans 2, recovers 0, leaves 2 pending, and the intents are
     still pending afterwards.
  B. Reference engine plus apply_capture_bundle (applied by the installed built-in
     SQLite capture code): the backend receives each turn's real bundle (same
     capture_id, root turn and session as the turn's intent), the turn's result is
     the backend's receipt, 2 results are recorded, recovery finds nothing; the
     backend's receipt for the same real bundle again is identical (replay); and an
     adapter-level bundle with one candidate returns one output, and its replay
     returns the same receipt without a second record.
  C. Reference engine whose apply_capture_bundle raises its own error: Agent.run
     raises that error (not CaptureRecoveryUnsupportedError), the intents stay
     pending, no result, recovery leaves them pending; adapter-level the same error
     propagates.
  D. Default memory provider (control): capture succeeds, 2 results, nothing pending.
  Every case: runtime.close() x3 closes the memory assembly once without error.

Usage: python -I -X utf8 probe_397.py <work_dir> <out.json>
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, write_result  # noqa: E402
from probe_388_lib import (  # noqa: E402
    CAPTURED_BUNDLES,
    CAPTURING_ADAPTER,
    FAILING_ADAPTER,
    CaptureFailed,
    external_config,
    register_backends,
    runtime_manager,
)

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
WORK.mkdir(parents=True, exist_ok=True)
quiet()
clear_override_env()
info = banner("probe_397")

from openminion import Agent, APIRuntime  # noqa: E402
from openminion.modules.memory.backends.builtin import BackendMemoryStoreAdapter  # noqa: E402
from openminion.modules.memory.errors import CaptureRecoveryUnsupportedError  # noqa: E402
from openminion.services.runtime import memory as runtime_memory  # noqa: E402

BACKENDS = register_backends()
os.environ["OPENMINION_HOME"] = str(WORK / "env-home")
os.environ["OPENMINION_DATA_ROOT"] = str(WORK / "env-home" / "data")
RuntimeMemoryAssembly = runtime_memory.RuntimeMemoryAssembly
RECEIPT_FIELDS = ("committed_at", "output_ids", "result_hash", "report_hash")

c = Checks()
observed: dict[str, object] = {}


def bundle_result(metadata: dict) -> dict:
    raw = metadata.get("memory_capture_bundle_result")
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except (TypeError, ValueError):
        return {"unparsed": raw}


def run_case(root: Path, adapter: str | None) -> dict:
    memory_config = None
    if adapter is not None:
        memory_config = external_config(root / "external.sqlite3", adapter,
                                        {"backend": "sqlite", "sqlite_path": str(root / "memory.db")})
    outcomes: list[str] = []
    assembly_close = RuntimeMemoryAssembly.close

    def recording_close(self):
        try:
            result = assembly_close(self)
            outcomes.append(str(result.reason_code))
            return result
        except Exception as exc:  # noqa: BLE001
            outcomes.append(f"raised {type(exc).__name__}: {exc}")
            raise

    RuntimeMemoryAssembly.close = recording_close  # type: ignore[method-assign]
    CAPTURED_BUNDLES.clear()
    row: dict[str, object] = {}
    runtime = APIRuntime.from_manager(runtime_manager(root, memory_config))
    try:
        [assembly] = list(runtime._memory_assemblies.values())
        store = assembly.service._store
        row["memory_service_store"] = type(store).__name__
        backend = getattr(store, "_backend", None)
        row["backend"] = type(backend).__name__ if backend is not None else None
        row["backend_has_apply_capture_bundle"] = callable(getattr(backend, "apply_capture_bundle", None))
        turns = []
        with Agent(runtime=runtime) as agent:
            for _ in range(2):
                try:
                    result = agent.run("hello", session_id="probe-397")
                    md = dict(result.metadata or {})
                    turns.append({"run_state": result.run_state,
                                  "memory_capture_state": md.get("memory_capture_state"),
                                  "memory_capture_bundle_result": bundle_result(md)})
                except Exception as exc:  # noqa: BLE001
                    turns.append({"raised": f"{type(exc).__name__}: {exc}",
                                  "is_capture_failed": isinstance(exc, CaptureFailed),
                                  "is_capture_recovery_unsupported": isinstance(exc, CaptureRecoveryUnsupportedError)})
        row["turns"] = turns
        sessions = runtime.sessions

        def events():
            return [e for s in sessions.list_sessions(limit=100)
                    for e in sessions.list_events(session_id=s.id, limit=1000)]

        evs = events()
        intents = [e for e in evs if e.event_type == "turn.outcome"]
        results = [e for e in evs if e.event_type == "memory.capture.result"]
        row["intents"] = [{"capture_id": e.payload.get("capture_id"), "capture_state": e.payload.get("capture_state"),
                           "root_turn_id": e.payload.get("root_turn_id"),
                           "runtime_session_id": e.payload.get("runtime_session_id")} for e in intents]
        row["capture_results"] = [dict(e.payload) for e in results]
        agent_id = str(intents[0].payload["agent_id"]) if intents else ""
        try:
            recovery = runtime.resolve_memory_assembly(agent_id).recover_pending_captures(
                sessions=sessions, agent_id=agent_id, extract_candidates=lambda *_: [], authorize=lambda *_: True)
            row["recovery"] = [recovery.scanned, recovery.recovered, recovery.pending]
        except Exception as exc:  # noqa: BLE001
            row["recovery"] = f"raised {type(exc).__name__}: {exc}"
        after = events()
        row["capture_results_after_recovery"] = sum(e.event_type == "memory.capture.result" for e in after)
        row["bundles_received_by_backend"] = [
            {"capture_id": b.capture_id, "root_turn_id": b.root_turn_id, "session_id": b.session_id,
             "agent_id": b.agent_id, "candidates": len(b.candidates)} for b in CAPTURED_BUNDLES]
        if adapter == CAPTURING_ADAPTER and CAPTURED_BUNDLES:
            replay = BackendMemoryStoreAdapter(backend).apply_capture_bundle(CAPTURED_BUNDLES[0])
            row["replay_of_first_real_bundle"] = {
                "capture_id": replay.capture_id, "disposition": str(getattr(replay, "disposition", "")),
                "output_ids": list(replay.output_ids), "result_hash": getattr(replay, "result_hash", None)}
    finally:
        closes = []
        for _ in range(3):
            try:
                runtime.close()
                closes.append("ok")
            except Exception as exc:  # noqa: BLE001
                closes.append(f"raised {type(exc).__name__}: {exc}")
        RuntimeMemoryAssembly.close = assembly_close  # type: ignore[method-assign]
        row["runtime_close_x3"] = closes
        row["assembly_close_outcomes"] = outcomes
    return row


def clean_shutdown(row: dict) -> bool:
    return row["runtime_close_x3"] == ["ok"] * 3 and row["assembly_close_outcomes"] == ["closed"]


def no_receipt(result: dict) -> bool:
    return not any(field in result for field in RECEIPT_FIELDS)


# ---------------------------------------------------------------- A. reference-sqlite
a = run_case(WORK / "A", "reference-sqlite")
observed["A_reference_sqlite_without_capture"] = a
c.check("A. two real SDK turns with the reference-sqlite external backend complete (no NotImplementedError)",
        a["backend"] == "SophiaGraphSqliteStore" and not a["backend_has_apply_capture_bundle"]
        and [t.get("run_state") for t in a["turns"]] == ["completed", "completed"],
        {"backend": a["backend"], "turns": a["turns"]})
c.check("A. each capture stays pending with CAPTURE_RECOVERY_UNSUPPORTED and no invented receipt; "
        "no memory.capture.result event",
        all(t.get("memory_capture_state") == "pending"
            and t["memory_capture_bundle_result"].get("error_code") == "CAPTURE_RECOVERY_UNSUPPORTED"
            and t["memory_capture_bundle_result"].get("disposition") == "pending"
            and no_receipt(t["memory_capture_bundle_result"]) for t in a["turns"] if "raised" not in t)
        and len(a["turns"]) == 2 and all("raised" not in t for t in a["turns"])
        and [i["capture_state"] for i in a["intents"]] == ["pending", "pending"] and a["capture_results"] == [],
        {"bundle_results": [t.get("memory_capture_bundle_result") for t in a["turns"]],
         "intents": a["intents"], "capture_results": a["capture_results"]})
c.check("A. recovery leaves the unsupported intents pending (scanned 2, recovered 0, pending 2; still no result)",
        a["recovery"] == [2, 0, 2] and a["capture_results_after_recovery"] == 0,
        {"recovery": a["recovery"], "results_after": a["capture_results_after_recovery"]})
c.check("A. runtime.close() x3: the memory assembly closes once, without error", clean_shutdown(a),
        {"closes": a["runtime_close_x3"], "assembly": a["assembly_close_outcomes"]})

# ---------------------------------------------------------------- B. backend implementing capture
b = run_case(WORK / "B", CAPTURING_ADAPTER)
observed["B_external_backend_with_capture"] = b
got = b["bundles_received_by_backend"]
want = [{"capture_id": i["capture_id"], "root_turn_id": i["root_turn_id"], "session_id": i["runtime_session_id"]}
        for i in b["intents"]]
c.check("B. the backend's apply_capture_bundle receives each turn's real bundle (capture_id, root turn and session "
        "of that turn's intent); both turns complete",
        [t.get("run_state") for t in b["turns"]] == ["completed", "completed"] and len(got) == 2
        and [{k: g[k] for k in ("capture_id", "root_turn_id", "session_id")} for g in got] == want,
        {"bundles": got, "intents": b["intents"]})
receipts = [t.get("memory_capture_bundle_result", {}) for t in b["turns"]]
c.check("B. each turn's result is the backend's receipt (no error_code), recorded as 2 memory.capture.result "
        "events with the same result hashes; recovery finds nothing pending",
        all("error_code" not in r and r.get("capture_id") == g["capture_id"] for r, g in zip(receipts, got))
        and [r.get("result_hash") for r in receipts] == [x.get("result_hash") for x in b["capture_results"]]
        and len(b["capture_results"]) == 2 and b["recovery"] == [0, 0, 0],
        {"receipts": receipts, "capture_results": b["capture_results"], "recovery": b["recovery"]})
rp = b.get("replay_of_first_real_bundle", {})
c.check("B. replaying the first real bundle on the backend returns the same receipt",
        rp.get("capture_id") == receipts[0].get("capture_id") and rp.get("result_hash") == receipts[0].get("result_hash")
        and rp.get("output_ids") == receipts[0].get("output_ids"), {"replay": rp, "turn_receipt": receipts[0]})
c.check("B. runtime.close() x3: the memory assembly closes once, without error", clean_shutdown(b),
        {"closes": b["runtime_close_x3"], "assembly": b["assembly_close_outcomes"]})

# Adapter level with a candidate: receipt with one output, replay without a second record.
from openminion.modules.memory.runtime.capture_bundle import CaptureBundleInput, CaptureCandidateInput  # noqa: E402

bundle = CaptureBundleInput(capture_id="capture:probe-397", root_turn_id="turn-1", session_id="session-1",
                            agent_id="agent-1",
                            candidates=(CaptureCandidateInput(kind="fact", normalized_key="fact:sky", title="Sky",
                                                              content="the sky is blue"),))
cap_backend = BACKENDS["capturing"](WORK / "B-adapter.sqlite3")
adapter = BackendMemoryStoreAdapter(cap_backend)
first = adapter.apply_capture_bundle(bundle)
second = BackendMemoryStoreAdapter(cap_backend).apply_capture_bundle(bundle)
cap_backend.close()
import sqlite3  # noqa: E402
from contextlib import closing  # noqa: E402

with closing(sqlite3.connect(WORK / "B-adapter.capture.db")) as conn:
    stored = {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
              for table in ("memory_candidates", "memory_capture_bundle_receipts")}
adapter_row = {"bundles_received": len(cap_backend.bundles), "first_receipt_outputs": list(first.output_ids),
               "replay_equals_first": second == first, "rows_in_backend_capture_store": stored}
observed["B_adapter_level_bundle_with_candidate"] = adapter_row
c.check("B. adapter level: a bundle with one candidate reaches the backend and returns its receipt with one output; "
        "the replay returns the same receipt and stores no second candidate or receipt",
        adapter_row["bundles_received"] == 2 and len(adapter_row["first_receipt_outputs"]) == 1
        and adapter_row["replay_equals_first"]
        and stored == {"memory_candidates": 1, "memory_capture_bundle_receipts": 1},
        adapter_row)

# ---------------------------------------------------------------- C. backend capture failure
cc = run_case(WORK / "C", FAILING_ADAPTER)
observed["C_external_backend_capture_fails"] = cc
c.check("C. a backend whose own apply_capture_bundle fails: Agent.run raises that error (CaptureFailed, not "
        "CaptureRecoveryUnsupportedError) on both turns; intents stay pending, no result, recovery leaves 2 pending",
        len(cc["turns"]) == 2 and all(t.get("is_capture_failed") and not t.get("is_capture_recovery_unsupported")
                                      for t in cc["turns"])
        and [i["capture_state"] for i in cc["intents"]] == ["pending", "pending"] and cc["capture_results"] == []
        and cc["recovery"] == [2, 0, 2],
        {"turns": cc["turns"], "intents": cc["intents"], "recovery": cc["recovery"]})
try:
    BackendMemoryStoreAdapter(BACKENDS["failing"](WORK / "C-adapter.sqlite3")).apply_capture_bundle(bundle)
    direct = "no error"
except Exception as exc:  # noqa: BLE001
    direct = f"{type(exc).__name__}: {exc}"
observed["C_adapter_level_failure"] = direct
c.check("C. adapter level: the backend's own error propagates unchanged",
        direct == "CaptureFailed: backend capture failed", direct)
c.check("C. runtime.close() x3: the memory assembly closes once, without error", clean_shutdown(cc),
        {"closes": cc["runtime_close_x3"], "assembly": cc["assembly_close_outcomes"]})

# ---------------------------------------------------------------- D. default provider
d = run_case(WORK / "D", None)
observed["D_default_memory_provider"] = d
c.check("D. default memory capture (control): both turns complete with a receipt, 2 results, nothing pending",
        [t.get("run_state") for t in d["turns"]] == ["completed", "completed"]
        and all("error_code" not in t["memory_capture_bundle_result"] for t in d["turns"] if "raised" not in t)
        and len(d["capture_results"]) == 2 and d["recovery"] == [0, 0, 0],
        {"turns": d["turns"], "results": len(d["capture_results"]), "recovery": d["recovery"]})
c.check("D. runtime.close() x3: the memory assembly closes once, without error", clean_shutdown(d),
        {"closes": d["runtime_close_x3"], "assembly": d["assembly_close_outcomes"]})

write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items,
                   "observed": observed})
s = c.summary()
print(f"SUMMARY passed={s['passed']} failed={s['failed']} total={s['total']}")
sys.exit(0 if c.all_pass else 1)
