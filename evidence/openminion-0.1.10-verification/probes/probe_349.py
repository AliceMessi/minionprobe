"""#349: the memory artifact controller the runtime obtains closes exactly once.

  A. SDK (the issue's probe, without repo fixtures): memory_v2, single-process echo,
     explicit roots; every ArtifactCtl counted; runtime.close() twice.
  B. Factory-supplied controller: build_memory_v2_runtime_assembly(artifactctl_factory=...)
     with a real ArtifactCtl; assembly.close() twice.
  C. Runtime-created default (no factory) through the same builder; close() twice.
  D. scheduler_still_running: a scheduler stub that stays alive after stop(); the
     controller and memory service stay open; after it stops, one close each.
  E. Nothing owned where nothing was obtained: factory returns None / raises;
     disabled, smoke and backend-none assemblies (default factory never called).

B-E use the installed builder `openminion.services.runtime.memory` directly: the SDK
(APIRuntime) exposes no way to inject a factory or a scheduler.

Usage: python -I probe_349.py <work_dir> <out.json>
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, echo_config, quiet, write_result  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
WORK.mkdir(parents=True, exist_ok=True)
quiet()
info = banner("probe_349")

from openminion import Agent, APIRuntime  # noqa: E402
from openminion.base.config import OpenMinionConfig  # noqa: E402
from openminion.modules.artifact.control import ArtifactCtl  # noqa: E402
from openminion.modules.artifact.config import ArtifactCtlConfig  # noqa: E402,F401
from openminion.services.runtime import memory as runtime_memory  # noqa: E402

c = Checks()
observed: dict[str, object] = {}

created: list[ArtifactCtl] = []
closed: list[int] = []
original_init, original_close = ArtifactCtl.__init__, ArtifactCtl.close


def record_init(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    created.append(self)


def record_close(self):
    closed.append(id(self))
    original_close(self)


ArtifactCtl.__init__ = record_init  # type: ignore[method-assign]
ArtifactCtl.close = record_close  # type: ignore[method-assign]


def closes(store) -> int:
    return closed.count(id(store))


# A. SDK ---------------------------------------------------------------------
memory_stores = []
create_memory_store = runtime_memory.create_default_artifactctl


def tag_memory_store(config=None):
    store = create_memory_store(config)
    memory_stores.append(store)
    return store


runtime_memory.create_default_artifactctl = tag_memory_store
root = WORK / "sdk"
os.environ.update({"OPENMINION_HOME": str(root), "OPENMINION_DATA_ROOT": str(root / "data")})
cfg = echo_config(root, memory_enabled=True, memory_provider="memory_v2")
mark = len(created)
runtime = APIRuntime.from_config_path(str(cfg), home_root=root, data_root=root / "data")
post_close_write = ""
try:
    with Agent(runtime=runtime) as agent:
        state = agent.run("hello").run_state
    runtime.close()
    counts_after_two = [closes(s) for s in created[mark:]]
    runtime.close()
    counts_after_three = [closes(s) for s in created[mark:]]
    if memory_stores:
        try:
            memory_stores[0].ingest_bytes(b"after-shutdown")
            post_close_write = "accepted"
        except Exception as exc:  # noqa: BLE001
            post_close_write = f"{type(exc).__name__}: {exc}"
finally:
    runtime.close()
    runtime_memory.create_default_artifactctl = create_memory_store
sdk_created = created[mark:]
observed["A_sdk"] = {
    "run_state": state,
    "artifactctl_instances": len(sdk_created),
    "memory_assembly_instance_index": sdk_created.index(memory_stores[0]) if memory_stores and memory_stores[0] in sdk_created else None,
    "close_counts_after_two_runtime_close": counts_after_two,
    "close_counts_after_third_runtime_close": counts_after_three,
    "ingest_bytes_on_memory_controller_after_shutdown": post_close_write,
}
c.check("A SDK: echo turn completed with memory_v2", state == "completed", state)
c.check("A SDK: exactly one controller obtained by the memory assembly, the first created", len(memory_stores) == 1 and bool(sdk_created) and sdk_created[0] is memory_stores[0], observed["A_sdk"]["memory_assembly_instance_index"])
c.check("A SDK: close counts [1, 1, 1] after runtime.close() twice", counts_after_two == [1, 1, 1], counts_after_two, [1, 1, 1])
c.check("A SDK: a third runtime.close() closes nothing again", counts_after_three == [1, 1, 1], counts_after_three)

# builder-level helpers ------------------------------------------------------
os.environ.update({"OPENMINION_HOME": str(WORK / "builder-home"), "OPENMINION_DATA_ROOT": str(WORK / "builder-home" / "data")})


def assembly(tmp: Path, **overrides):
    config = OpenMinionConfig()
    config.runtime.memory_enabled = True
    config.runtime.memory_provider = "memory_v2"
    tmp.mkdir(parents=True, exist_ok=True)
    args = {
        "config": config, "agent_id": "alpha", "memory_root": tmp,
        "logger": logging.getLogger("probe.349"), "config_manager": None, "home_root": None,
        "data_root": None, "session_context": None, "retrieve_ctl": None, "storage_path": None,
    }
    return runtime_memory.build_memory_v2_runtime_assembly(**{**args, **overrides})


def real_controller(tmp: Path) -> ArtifactCtl:
    # explicit controller config; ArtifactCtl requires its paths under the data root
    tmp = WORK / "builder-home" / "data" / tmp.name
    return ArtifactCtl(
        {"blob_store": {"root_dir": str(tmp / "artifact")}, "index": {"sqlite_path": str(tmp / "artifact" / "index.db")}}
    )


class CountingService:
    def __init__(self, service):
        self.inner = service
        self.closes = 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def close(self):
        self.closes += 1
        return self.inner.close()


class Scheduler:
    def __init__(self):
        self.live = True
        self.stops = 0

    def bind_record_source(self, record_source):
        pass

    def start(self):
        pass

    def stop(self):
        self.stops += 1

    def is_alive(self):
        return self.live


# B. factory-supplied --------------------------------------------------------
factory_store = []


def factory():
    store = real_controller(WORK / "factory")
    factory_store.append(store)
    return store


a = assembly(WORK / "B-mem", artifactctl_factory=factory)
first, second = a.close(), a.close()
observed["B_factory"] = {"recorded_is_factory_result": a.artifactctl is factory_store[0], "first": repr(first), "second_is_first": second is first, "close_count": closes(factory_store[0])}
c.check("B factory: the assembly records the factory's controller", len(factory_store) == 1 and a.artifactctl is factory_store[0])
c.check("B factory: close() twice -> closed, same result, controller closed exactly once", first.closed and second is first and closes(factory_store[0]) == 1, observed["B_factory"])

# C. runtime-created default (no factory) ------------------------------------
mark = len(created)
a = assembly(WORK / "C-mem")
default_store = a.artifactctl
first, second = a.close(), a.close()
observed["C_default"] = {"recorded": type(default_store).__name__, "created_during_build": len(created) - mark, "first": repr(first), "second_is_first": second is first, "close_count": closes(default_store) if default_store is not None else None}
c.check("C default: the assembly records the controller it created", isinstance(default_store, ArtifactCtl) and default_store in created[mark:])
c.check("C default: close() twice -> closed, same result, controller closed exactly once", first.closed and second is first and closes(default_store) == 1, observed["C_default"])

# D. scheduler still running -------------------------------------------------
for label, kwargs in (("factory", {"artifactctl_factory": lambda: real_controller(WORK / "D-factory")}), ("default", {})):
    sched = Scheduler()
    a = assembly(WORK / f"D-mem-{label}", scheduler=sched, **kwargs)
    service = CountingService(a.service)
    a.service = service
    store = a.artifactctl
    blocked = a.close()
    blocked_counts = (closes(store), service.closes)
    blocked_again = a.close()
    usable_while_blocked = ""
    try:
        store.ingest_bytes(b"while-scheduler-live")
        usable_while_blocked = "accepted"
    except Exception as exc:  # noqa: BLE001
        usable_while_blocked = f"{type(exc).__name__}: {exc}"
    sched.live = False
    done = a.close()
    done_again = a.close()
    after_counts = (closes(store), service.closes)
    observed[f"D_scheduler_{label}"] = {
        "blocked": repr(blocked), "blocked_again": repr(blocked_again),
        "controller_closes_while_blocked": blocked_counts[0], "service_closes_while_blocked": blocked_counts[1],
        "controller_write_while_blocked": usable_while_blocked,
        "after_stop": repr(done), "repeat_is_same": done_again is done,
        "controller_closes_after": after_counts[0], "service_closes_after": after_counts[1], "scheduler_stop_calls": sched.stops,
    }
    c.check(f"D scheduler ({label}): close() while live -> closed=False scheduler_still_running, twice",
            (blocked.closed, blocked.reason_code, blocked_again.closed, blocked_again.reason_code) == (False, "scheduler_still_running", False, "scheduler_still_running"),
            (blocked.reason_code, blocked_again.reason_code))
    c.check(f"D scheduler ({label}): controller and memory service stay open while live (0 closes, controller still accepts writes)",
            blocked_counts == (0, 0) and usable_while_blocked == "accepted", {"closes": blocked_counts, "write": usable_while_blocked})
    c.check(f"D scheduler ({label}): after the scheduler stops, close() closes service then controller once; repeat is a no-op",
            done.closed and done_again is done and after_counts == (1, 1), {"closes": after_counts})

# E. nothing obtained, nothing owned ------------------------------------------
def raising_factory():
    raise OSError("artifact root unavailable")


for label, fac in (("returns None", lambda: None), ("raises", raising_factory)):
    a = assembly(WORK / f"E-{label.replace(' ', '-')}", artifactctl_factory=fac)
    observed[f"E_factory_{label}"] = {"artifactctl": repr(a.artifactctl)}
    res = a.close()
    c.check(f"E factory {label}: assembly owns no controller, close() succeeds", a.artifactctl is None and res.closed, repr(a.artifactctl))

default_calls = []
runtime_memory.create_default_artifactctl = lambda config=None: default_calls.append(config)
try:
    disabled = OpenMinionConfig()
    disabled.runtime.memory_enabled = False
    smoke = OpenMinionConfig()
    smoke.runtime.memory_provider = "memory_v2_smoke"
    gateway_only = [
        runtime_memory.build_runtime_memory_assembly(config=cfg_, agent_id="alpha", memory_root=WORK / "E-gw", logger=logging.getLogger("probe.349"))
        for cfg_ in (disabled, smoke)
    ]
    gateway_only.append(assembly(WORK / "E-none", resolve_runtime_memory_config_fn=lambda **kw: {"backend": {"provider": "none"}}))
finally:
    runtime_memory.create_default_artifactctl = create_memory_store
labels = ["disabled", "smoke", "backend-none"]
for lab, asm in zip(labels, gateway_only):
    c.check(f"E {lab} assembly: owns no controller, close() succeeds", asm.artifactctl is None and asm.close().closed)
c.check("E disabled/smoke/backend-none: the default controller factory is never called", default_calls == [], len(default_calls))

ArtifactCtl.__init__ = original_init  # type: ignore[method-assign]
ArtifactCtl.close = original_close  # type: ignore[method-assign]
for s in created:
    if id(s) not in closed:
        try:
            original_close(s)
        except Exception:  # noqa: BLE001
            pass

result = {**info, "observed": observed, "checks": c.items, "all_pass": c.all_pass}
import json  # noqa: E402

write_result(OUT, json.loads(json.dumps(result, default=str).replace(str(WORK).replace("\\", "\\\\"), "<WORK>")))
print("ALL PASS" if c.all_pass else "SOME CHECKS FAILED")
raise SystemExit(0 if c.all_pass else 1)
