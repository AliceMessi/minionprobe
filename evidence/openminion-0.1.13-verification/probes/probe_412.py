"""#412 on the installed package: `storagectl restore` leaves the target equal to the snapshot.

    python -I -X utf8 probe_412.py <work_dir> <out.json>

The package ships no `storagectl` console script; storagectl is
`python -m openminion.modules.storage` (its `__main__` calls
`openminion.modules.storage.cli.main`, argparse prog `storagectl`). Every
storagectl command below is that entry point, run as its own process with
OPENMINION_HOME and OPENMINION_DATA_ROOT under the case folder.

Each case runs in its own folder:
1. `storagectl migrate --namespace session --sqlite db/session.db`;
2. seed (probe_412_child.py seed, its own process, which exits before the
   backup): two sessions with turns, events and working state through the
   installed SQLiteSessionStore, plus a probe_marker table (text, integer,
   real, blob and NULL values; row 1 = 'before-backup');
3. `storagectl backup --namespace session --sqlite db/session.db --snapshot-root snapshots`;
4. the probe changes the live database: marker row 1 = 'after-backup',
   marker row 2 and the last turn deleted, a new table added;
5. `storagectl restore --namespace session --sqlite <target> --snapshot-path <snapshot>`
   onto the live database (existing target) or onto db/restored.db (new path),
   or from a snapshot path that does not exist.

The restore runs either
- as the plain entry point (`python -I -X utf8 -m openminion.modules.storage restore ...`), or
- in probe_412_child.py with `--progress`: cyclic GC off before openminion is
  imported and gc.collect counted (0 expected), a recording progress reporter,
  the sqlite3 connections open on the target listed at the moment it is
  replaced, and, right after the command in the same process, the
  connections left open on the target and an immediate rename, replace and
  delete of the target file.

Afterwards the probe compares the target with the snapshot: every table and
schema object, every row of every table, the identity pragmas and header
fields (application_id, user_version, schema_version, page size, encoding,
auto_vacuum, WAL flag), the om_meta and alembic_version rows, the marker rows,
iterdump and the file bytes; and it lists the files next to the target (no
*.restore.tmp left). For a missing snapshot: the exit code, stderr, stdout,
progress calls, the existing target's bytes and contents, and that no
database or other file appears at a new path.
"""

from __future__ import annotations

import importlib.metadata as metadata
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, sanitize, write_result  # noqa: E402
from probe_412_lib import (  # noqa: E402
    case_env,
    child,
    content,
    differences,
    files_in,
    identity,
    json_payload,
    last_line,
    mutate_after_backup,
    restore_tmp_files,
    sha256,
    shown_cmd,
    storagectl,
)

import openminion  # noqa: E402
from openminion.modules.storage.migrations import MigrationRunner  # noqa: E402
from openminion.modules.storage.migrations.module_ids import get_module_application_id  # noqa: E402

WORK = Path(sys.argv[1])
OUT = Path(sys.argv[2])

# key: (target, how the restore runs, snapshot)
CASES = {
    "A": ("existing", "plain", "snapshot"),
    "B": ("new", "plain", "snapshot"),
    "C": ("existing", "gc-off", "snapshot"),
    "D": ("new", "gc-off", "snapshot"),
    "E": ("existing", "plain", "missing"),
    "F": ("new", "plain", "missing"),
    "G": ("existing", "gc-off", "missing"),
    "H": ("new", "gc-off", "missing"),
}
TARGET_TEXT = {"existing": "existing target", "new": "new path"}
RUN_TEXT = {"plain": "storagectl process", "gc-off": "GC-off process, --progress"}


def label(key: str) -> str:
    target, run, snapshot = CASES[key]
    what = "restore" if snapshot == "snapshot" else "restore from a missing snapshot"
    return f"[{key}: {what}, {TARGET_TEXT[target]}, {RUN_TEXT[run]}]"


def detect_dict(db: Path) -> dict:
    return MigrationRunner(
        module_id="session", db_path=db, module_application_id=get_module_application_id("session")
    ).detect().to_dict()


def setup_case(case: Path) -> dict:
    db_dir = case / "db"
    db_dir.mkdir(parents=True)
    live = db_dir / "session.db"
    env = case_env(case)
    out: dict = {"env": {"OPENMINION_HOME": "<case>/home", "OPENMINION_DATA_ROOT": "<case>/data"}}
    mig = storagectl(env, ["migrate", "--namespace", "session", "--sqlite", str(live)])
    payload, _ = json_payload(mig["stdout"])
    out["migrate"] = {"cmd": shown_cmd(mig["cmd"]), "exit_code": mig["exit_code"],
                      "ok": (payload or {}).get("ok"),
                      "application_id_matches": ((payload or {}).get("state") or {}).get("application_id_matches"),
                      "stderr last line": last_line(mig["stderr"])}
    seed = child(env, "seed", case / "seed.json", live)
    out["seed"] = {"exit_code": seed["exit_code"], "stderr last line": last_line(seed["stderr"]),
                   **({k: v for k, v in seed["result"].items() if k != "mode"} if seed["result"] else {})}
    bak = storagectl(env, ["backup", "--namespace", "session", "--sqlite", str(live),
                           "--snapshot-root", str(case / "snapshots")])
    payload, _ = json_payload(bak["stdout"])
    snapshot = Path(payload["backup"]["snapshot_path"]) if payload and payload.get("ok") else None
    out["backup"] = {"cmd": shown_cmd(bak["cmd"]), "exit_code": bak["exit_code"], "ok": (payload or {}).get("ok"),
                     "snapshot": snapshot.name if snapshot else None, "stderr last line": last_line(bak["stderr"])}
    out["_live"], out["_snapshot"], out["_env"] = live, snapshot, env
    return out


def run_case(root: Path, key: str, checks: Checks) -> dict:
    target_kind, run_kind, snapshot_kind = CASES[key]
    tag = label(key)
    case = root / key
    row = setup_case(case)
    live, snapshot, env = row.pop("_live"), row.pop("_snapshot"), row.pop("_env")
    db_dir = live.parent
    if snapshot is None:
        checks.check(f"{tag} setup: storagectl migrate and backup", False, row)
        return row

    snap = content(snapshot)
    mutate_after_backup(live)
    live_after_change = content(live)
    target = live if target_kind == "existing" else db_dir / "restored.db"
    restore_from = snapshot if snapshot_kind == "snapshot" else case / "snapshots" / "missing.sqlite"
    row["snapshot"] = {k: snap[k] for k in ("bytes", "header", "pragmas", "integrity_check", "schema objects",
                                           "total rows", "om_meta", "alembic_version", "probe_marker")}
    row["snapshot"]["tables (rows)"] = {t: snap["rows"][t]["rows"] for t in snap["tables"]}
    row["live database after the change, compared with the snapshot"] = differences(live_after_change, snap)
    seeded = (row.get("seed") or {}).get("rows per table after the seed") or {}
    setup_ok = (row["migrate"]["exit_code"] == 0 and row["migrate"]["ok"] is True and row["migrate"]["application_id_matches"] is True
                and row["seed"]["exit_code"] == 0 and row["backup"]["exit_code"] == 0 and row["backup"]["ok"] is True
                and all(snap["rows"].get(t, {}).get("rows") == n for t, n in seeded.items())
                and snap["probe_marker"][0][1] == "before-backup" and snap["integrity_check"] == "ok"
                and live_after_change["probe_marker"][0][1] == "after-backup"
                and "probe_after_backup" in live_after_change["tables"])
    checks.check(f"{tag} setup: storagectl migrate and backup exit 0 with ok: true; the snapshot holds the seeded rows "
                 f"and marker 'before-backup'; the live database then differs from it",
                 setup_ok, {"migrate": row["migrate"], "seed": row["seed"], "backup": row["backup"],
                            "snapshot tables": len(snap["tables"]), "snapshot rows": snap["total rows"],
                            "live database vs snapshot": sorted(row["live database after the change, compared with the snapshot"])})

    before_files = files_in(db_dir)
    before_target = content(target) if target.exists() else None
    row["before the restore"] = {"target exists": target.exists(), "files next to the target": before_files,
                                 "target sha256": before_target["sha256"] if before_target else None}
    args = ["restore", "--namespace", "session", "--sqlite", str(target), "--snapshot-path", str(restore_from)]
    if run_kind == "plain":
        res = storagectl(env, args)
        detail = None
    else:
        res = child(env, "cli", case / "cli.json", target, ["--progress", *args])
        detail = res["result"]
    payload, payload_error = json_payload(res["stdout"]) if res["stdout"].strip() else (None, None)
    row["restore"] = {"cmd": shown_cmd(res["cmd"]), "exit_code": res["exit_code"],
                      "stdout empty": res["stdout"].strip() == "", "stdout JSON keys": list(payload) if payload else None,
                      "stdout JSON error": payload_error, "stdout": payload,
                      "stderr last line": last_line(res["stderr"])}
    if detail is not None:
        row["restore"]["child"] = detail

    after_files = files_in(db_dir)
    leftovers = restore_tmp_files(db_dir)
    snap_unchanged = sha256(snapshot) == snap["sha256"]
    row["after the restore"] = {"target exists": target.exists(), "files next to the target": after_files,
                                "*.restore.tmp next to the target": leftovers,
                                "snapshot bytes unchanged": snap_unchanged}

    if snapshot_kind == "snapshot":
        got = content(target) if target.exists() else None
        diff = differences(got, snap) if got else {"target": "missing"}
        row["after the restore"]["target compared with the snapshot"] = diff or "identical"
        if got:
            row["after the restore"]["target"] = {"tables": len(got["tables"]), "rows": got["total rows"],
                                                 "identity": identity(got), "probe_marker": got["probe_marker"]}
        expected_state = detect_dict(snapshot)
        state = (payload or {}).get("state") or {}
        state_ok = (res["exit_code"] == 0 and payload is not None and list(payload) == ["ok", "state"]
                    and payload.get("ok") is True and state.get("application_id_matches") is True
                    and {**state, "db_path": None} == {**expected_state, "db_path": None}
                    and Path(str(state.get("db_path"))).resolve() == target.resolve())
        checks.check(f"{tag} exits 0 and prints only the JSON {{ok, state}}: ok true, application_id_matches true, "
                     f"state equal to the snapshot's MigrationRunner.detect() apart from db_path, which is the target",
                     state_ok, {"exit_code": res["exit_code"], "stdout JSON keys": row["restore"]["stdout JSON keys"],
                                "state": state, "stderr last line": row["restore"]["stderr last line"]})
        checks.check(f"{tag} the target has all snapshot tables and schema objects, and no others",
                     bool(got) and got["tables"] == snap["tables"] and got["schema sha256"] == snap["schema sha256"],
                     {"target tables": len(got["tables"]) if got else None, "snapshot tables": len(snap["tables"]),
                      "target schema objects": got["schema objects"] if got else None, "snapshot schema objects": snap["schema objects"],
                      **{k: v for k, v in diff.items() if "tables" in k and "rows" not in k}})
        checks.check(f"{tag} every table holds exactly the snapshot's rows",
                     bool(got) and got["rows"] == snap["rows"] and got["iterdump sha256"] == snap["iterdump sha256"],
                     {"target rows": got["total rows"] if got else None, "snapshot rows": snap["total rows"],
                      **{k: v for k, v in diff.items() if "rows" in k or k == "iterdump sha256"}})
        checks.check(f"{tag} identity pragmas, header fields, om_meta and alembic_version equal the snapshot's",
                     bool(got) and identity(got) == identity(snap),
                     {"target": identity(got) if got else None, "snapshot": identity(snap)})
        checks.check(f"{tag} the marker rows equal the snapshot's (row 1 'before-backup')",
                     bool(got) and got["probe_marker"] == snap["probe_marker"],
                     {"target": [r[:2] for r in got["probe_marker"]] if got and got["probe_marker"] else None,
                      "snapshot": [r[:2] for r in snap["probe_marker"]]})
        checks.check(f"{tag} the target's bytes equal the snapshot's, integrity_check ok, snapshot unchanged",
                     bool(got) and got["sha256"] == snap["sha256"] and got["integrity_check"] == "ok" and snap_unchanged,
                     {"target sha256": got["sha256"] if got else None, "snapshot sha256": snap["sha256"],
                      "integrity_check": got["integrity_check"] if got else None, "snapshot unchanged": snap_unchanged})
        expected_files = sorted(set(before_files) | {target.name})
        checks.check(f"{tag} no restore temporary file is left and no other file appears next to the target",
                     not leftovers and after_files == expected_files,
                     {"*.restore.tmp": leftovers, "files before": before_files, "files after": after_files})
    else:
        failure_ok = (res["exit_code"] == 1 and res["stdout"].strip() == ""
                      and "BackupError: Snapshot not found" in row["restore"]["stderr last line"])
        checks.check(f"{tag} fails: exit 1 with BackupError: Snapshot not found, nothing on stdout (no JSON payload)",
                     failure_ok, {"exit_code": res["exit_code"], "stdout empty": row["restore"]["stdout empty"],
                                  "stderr last line": row["restore"]["stderr last line"]})
        if target_kind == "existing":
            got = content(target) if target.exists() else None
            same = bool(got) and got["sha256"] == before_target["sha256"] and not differences(got, before_target)
            row["after the restore"]["target compared with before the restore"] = (differences(got, before_target) or "identical") if got else "missing"
            checks.check(f"{tag} the existing target is preserved: same bytes, tables, rows and marker ('after-backup')",
                         same and got["probe_marker"][0][1] == "after-backup",
                         {"sha256 before": before_target["sha256"], "sha256 after": got["sha256"] if got else None,
                          "marker after": got["probe_marker"][0][:2] if got and got["probe_marker"] else None})
        else:
            checks.check(f"{tag} no database is created at the new path",
                         not target.exists(), {"target exists": target.exists()})
        checks.check(f"{tag} no restore temporary file and no new file next to the target",
                     not leftovers and after_files == before_files,
                     {"*.restore.tmp": leftovers, "files before": before_files, "files after": after_files})

    if detail is not None:
        after = detail["after the command"]
        progress = detail["progress calls"]
        success = snapshot_kind == "snapshot"
        checks.check(f"{tag} progress: the reporter got start 'restore[session]' and end success={success}",
                     progress == [["start", "restore[session]"], ["end", success]] and detail["progress reporter selections"] == 1,
                     {"progress calls": progress, "reporter selections": detail["progress reporter selections"]})
        replaced = detail["replacements of the target"]
        if success:
            at_replace = [r["open connections on the target at the replacement"]["count"] for r in replaced]
            checks.check(f"{tag} at the replacement no sqlite3 connection was open on the target "
                         f"(nothing collected) and the replace succeeded",
                         len(replaced) == 1 and at_replace == [0] and replaced[0]["replace"] == "ok"
                         and replaced[0]["open connections on the target at the replacement"]["connections that could not be inspected"] == 0,
                         replaced)
        else:
            checks.check(f"{tag} no replacement of the target was attempted", replaced == [], replaced)
        ops = after.get("operations right after the command") or {}
        handles_ok = (after["open connections on the target"]["count"] == 0
                      and after["open connections on the target"]["connections that could not be inspected"] == 0
                      and all(v == "ok" for v in ops.values())
                      and after.get("target sha256 before the operations") == after.get("target sha256 after the operations"))
        if after["target exists"]:
            handles_ok = handles_ok and set(ops) >= {"rename the target away", "rename it back",
                                                      "replace the target with a byte copy (os.replace)", "delete the target"}
        checks.check(f"{tag} right after the command, same process, GC off: no connection open on the target"
                     + ("; the file can be renamed, replaced and deleted at once" if after["target exists"] else ""),
                     handles_ok, {"open connections": after["open connections on the target"],
                                  "opened on the target during the command": after["connections opened on the target during the command"],
                                  "operations": ops})
        checks.check(f"{tag} cyclic GC stayed off in the command's process and gc.collect was never called",
                     detail["gc"] == {"enabled": False, "gc.collect calls": 0}, detail["gc"])
    return row


def main() -> int:
    quiet()
    clear_override_env()
    storage_pkg = Path(openminion.__file__).resolve().parent / "modules" / "storage"
    scripts = sorted(ep.name for ep in metadata.entry_points(group="console_scripts") if "storage" in ep.name)
    info = banner(
        "probe_412",
        openminion_dist_version=metadata.version("openminion"),
        storagectl_entry_point="python -m openminion.modules.storage (argparse prog 'storagectl')",
        storage_main_module_installed=(storage_pkg / "__main__.py").exists(),
        console_scripts_named_like_storage=scripts,
    )
    checks = Checks()
    results: dict = {}
    root = WORK / "run"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    for key in CASES:
        print(f"== case {label(key)}", flush=True)
        try:
            results[label(key)] = run_case(root, key, checks)
        except Exception as exc:  # noqa: BLE001 - a probe error is a failed check, not a crash
            checks.check(f"{label(key)} ran to the end", False, f"{type(exc).__name__}: {exc}")
    results["working directory entries at the end"] = sorted(p.name for p in Path.cwd().iterdir())
    summary = checks.summary()
    write_result(OUT, {"info": info, "results": sanitize(results), "checks": checks.items, "summary": summary})
    print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
    return 0 if checks.all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
