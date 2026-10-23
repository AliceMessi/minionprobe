"""#342: explicit SDK home/data roots govern every runtime artifact construction site.

Two echo-provider runtimes on explicit roots, while the process cwd and an
unrelated home each hold a populated default artifact store (OPENMINION_* unset,
HOME/USERPROFILE and Path.home redirected). Every ArtifactCtl opened while the
runtimes are alive is recorded with its index path and its construction site.

Usage: python -I probe_342.py <work_dir> <out.json>
"""

from __future__ import annotations

import hashlib
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import OVERRIDE_ENV_KEYS, Checks, banner, echo_config, quiet, write_result  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
ALIAS = "shared-note"

for key in OVERRIDE_ENV_KEYS:
    os.environ.pop(key, None)
home = WORK / "home"
cwd = WORK / "cwd"
for d in (home, cwd):
    d.mkdir(parents=True, exist_ok=True)
os.environ["HOME"] = str(home)
os.environ["USERPROFILE"] = str(home)
Path.home = classmethod(lambda cls: home)  # type: ignore[method-assign]

quiet()
info = banner("probe_342")
from openminion import Agent, APIRuntime  # noqa: E402
from openminion.modules.artifact.control import ArtifactCtl  # noqa: E402


def index_of(root: Path) -> Path:
    return (root / ".openminion" / "artifact" / "index.db").resolve()


def snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def dirs(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir())


# Unrelated populated default stores: ArtifactCtl({}) falls back to the cwd.
unrelated = {}
for place in (home, cwd):
    os.chdir(place)
    with ArtifactCtl({}) as store:
        ref = store.ingest_bytes(f"UNRELATED_{place.name}_ARTIFACT".encode())
        store.alias_set(ALIAS, ref.sha256)
        unrelated[place.name] = ref.sha256
os.chdir(cwd)  # the runtimes run with this cwd
before = {"home": snapshot(home), "cwd": snapshot(cwd)}
dirs_before = {"home": dirs(home), "cwd": dirs(cwd)}
dirs_after_construction: dict[str, list[str]] = {}

opened: list[dict[str, str]] = []
original_init = ArtifactCtl.__init__


def construction_site() -> str:
    for frame in reversed(traceback.extract_stack()[:-2]):
        name = frame.filename.replace("\\", "/")
        if "/openminion/" in name and "/openminion/modules/artifact/" not in name:
            rel = name.split("/site-packages/openminion/")[-1]
            return f"{rel}::{frame.name}"
    return "outside openminion"


def record_init(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    opened.append({"index": str(Path(self.config.index.sqlite_path).resolve()), "site": construction_site(), "id": id(self)})


ArtifactCtl.__init__ = record_init  # type: ignore[method-assign]

roots = {"a": WORK / "root-a", "b": WORK / "root-b"}
runtimes = {}
first_alias = {}
written = {}
read_back = {}
listed = {}
run_states = {}
store_source = {}
sites_by_runtime: dict[str, list[dict[str, str]]] = {}
try:
    for name, root in roots.items():
        mark = len(opened)
        config_path = echo_config(root)
        runtime = APIRuntime.from_config_path(str(config_path), home_root=root, data_root=root / ".openminion")
        runtimes[name] = runtime
        if name == "a":
            dirs_after_construction = {k: sorted(set(dirs(p)) - set(dirs_before[k])) for k, p in (("home", home), ("cwd", cwd))}
        with Agent(runtime=runtime, session_id=f"artifact-{name}") as agent:
            run_states[name] = agent.run("hello").run_state
        sites_by_runtime[name] = opened[mark:]
        store = getattr(getattr(runtime, "agent", None), "artifactctl", None)
        store_source[name] = "runtime.agent.artifactctl"
        if store is None:
            raise RuntimeError("runtime.agent.artifactctl not available")
        first_alias[name] = store.alias_resolve(ALIAS)
        written[name] = store.ingest_bytes(f"ROOT_{name}_ARTIFACT".encode()).sha256
        store.alias_set(ALIAS, written[name])
    for name, runtime in runtimes.items():
        store = runtime.agent.artifactctl
        read_back[name] = store.read_bytes(store.alias_resolve(ALIAS).sha256).decode()
        listed[name] = sorted(meta.sha256 for meta in store.list_recent(limit=500))
finally:
    for runtime in runtimes.values():
        runtime.close()
    ArtifactCtl.__init__ = original_init  # type: ignore[method-assign]

after = {"home": snapshot(home), "cwd": snapshot(cwd)}
new_dirs = {k: sorted(set(dirs(p)) - set(dirs_before[k])) for k, p in (("home", home), ("cwd", cwd))}
new_dir_files = {k: [f for f in after[k] if any(f.startswith(d + "/") for d in new_dirs[k])] for k in new_dirs}
c = Checks()
for name in roots:
    c.check(f"runtime {name}: echo turn completed", run_states.get(name) == "completed", run_states.get(name))
    c.check(f"runtime {name}: unrelated cwd/home alias '{ALIAS}' not visible before writing", first_alias.get(name) is None, repr(first_alias.get(name)))
    c.check(f"runtime {name}: reads back only its own artifact under the shared alias", read_back.get(name) == f"ROOT_{name}_ARTIFACT", read_back.get(name))
other = {"a": "b", "b": "a"}
for name in roots:
    c.check(
        f"runtime {name}: lists its own artifact, not the other runtime's nor the unrelated stores'",
        written[name] in listed[name]
        and written[other[name]] not in listed[name]
        and not (set(unrelated.values()) & set(listed[name])),
        {"own": written[name] in listed[name], "other": written[other[name]] in listed[name], "unrelated": sorted(set(unrelated.values()) & set(listed[name]))},
    )
indexes = sorted({entry["index"] for entry in opened})
expected_indexes = sorted({str(index_of(roots["a"])), str(index_of(roots["b"]))})
c.check("every ArtifactCtl opened by the runtimes uses an index under its own data root", indexes == expected_indexes, indexes, expected_indexes)
for name, root in roots.items():
    own = str(index_of(root))
    sites = sorted({entry["site"] for entry in sites_by_runtime[name]})
    c.check(
        f"runtime {name}: all construction sites during the turn open <root>/.openminion/artifact/index.db ({len(sites_by_runtime[name])} instances)",
        all(entry["index"] == own for entry in sites_by_runtime[name]) and len(sites_by_runtime[name]) >= 3,
        sites,
    )
    c.check(f"runtime {name}: index exists at <root>/.openminion/artifact/index.db", index_of(root).is_file())
c.check("unrelated home store byte-for-byte unchanged, nothing new written under home", after["home"] == before["home"], sorted(after["home"]))
c.check("process cwd store byte-for-byte unchanged, nothing new written under cwd", after["cwd"] == before["cwd"], sorted(after["cwd"]))

result = {
    **info,
    "work_dir": "<WORK>",
    "store_under_test": store_source,
    "artifactctl_instances": [
        {"runtime": rt, "site": e["site"], "index": e["index"].replace(str(WORK), "<WORK>")}
        for rt, entries in sites_by_runtime.items()
        for e in entries
    ],
    "all_opened_indexes": [i.replace(str(WORK), "<WORK>") for i in indexes],
    "unrelated_store_files": {k: sorted(v) for k, v in before.items()},
    "informational_new_directories": {
        "note": "Not part of the #342 check (ArtifactCtl/artifact index). Empty directories created by the runtime under cwd/home, listed for completeness.",
        "after_runtime_a_construction": dirs_after_construction,
        "after_both_runtimes_closed": new_dirs,
        "files_inside_new_directories": new_dir_files,
    },
    "checks": c.items,
    "all_pass": c.all_pass,
}
result["cwd"] = "<WORK>/cwd"
write_result(OUT, result)
print("ALL PASS" if c.all_pass else "SOME CHECKS FAILED")
raise SystemExit(0 if c.all_pass else 1)
