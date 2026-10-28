"""#450 on the installed package: `python -m openminion.modules.storage` exits
with the status that storagectl's `main()` returns.

    python -I -X utf8 probe_450.py <work_dir> <out.json> [<baseline.json>]

<baseline.json> is optional: the output of this probe on the published 0.1.13
(the negative control). With it, the module's output is also compared with
0.1.13's, case by case.

1. Package: openminion is imported from this venv's site-packages (the
   installed wheel, not a checkout), in this process and in a child started
   like every command below (`python -I -X utf8`, empty working directory).
   The installed `modules/storage/__main__.py` and `cli.py` match the wheel's
   RECORD. The package has no storagectl console script; the existing CLI
   entry point is `python -m openminion.modules.storage.cli`, since `cli.py`
   ends with `raise SystemExit(main())`.
2-5. Each case runs three child processes with the same interpreter
   (`python -I -X utf8`), the same arguments, the same environment and fresh
   copies of the same files, each in its own new empty working directory:
     module : python -m openminion.modules.storage ARGS          (the fix)
     cli    : python -m openminion.modules.storage.cli ARGS      (existing entry point)
     main() : python -c CALL_MAIN SIDE ARGS
              calls openminion.modules.storage.cli.main(ARGS) and writes what
              it returned or raised to SIDE, so its stdout and stderr are what
              main() printed.
   The child environment drops OPENMINION_STORAGE_POSTGRES_URL (and the test
   URL) and points OPENMINION_HOME / OPENMINION_DATA_ROOT at the work dir.
   Cases 1-4 are the maintainer's checklist cases; cases 5-10 are the other
   paths reproduced in the issue.
   - 2: `plan --namespace session` exits 0 (main() returns 0, JSON ok true).
   - 3: `pool-stats` without a PostgreSQL URL and `a2a-archive-old
     --older-than-days 0` exit 2 and print exactly the existing JSON
     ({"ok": false, "error": ...}), as main() prints it, with empty stderr.
   - 4: `plan --namespace unknown-module` exits 1, stdout empty, stderr is
     the traceback ending in `ValueError: <the message main() raised>`.
   - 5: in every case, the module and cli exit with the same status as each
     other and as main() (returned value; 1 for an exception; the code of
     SystemExit), print the same stdout as each other and as main() (raw
     bytes), the same stderr (for a raising command: the same lines before the
     traceback and the same traceback from storagectl's main() frame on; only
     the entry-point frames differ), and leave the same files. Every failing
     case exits nonzero from both entry points.
   - with <baseline.json>: the module's stdout is identical to 0.1.13's in
     every case, and its stderr too (raising commands: apart from the entry
     point frames); the exit status transitions are recorded.

With OPENMINION_TEST_POSTGRES_URL set (the Linux runner's PostgreSQL 16), a
control outside the checklist also runs `pool-stats --postgres-url <that URL>`
through the three entry points: main() returns 0, both modules exit 0 and print
ok true with the pool stats. Any PostgreSQL URL is written as <PGURL>.

Paths: the case work dir is written as <RUN_WORK> in stored outputs, so runs in
different work dirs compare equal; other local prefixes are sanitized by
_common. Stored stdout/stderr have CRLF written as LF; the comparisons between
entry points use the raw bytes (sha256 recorded).
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):  # LF logs on Windows too (evidence is published byte-for-byte)
    _stream.reconfigure(newline="\n")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, sanitize, sanitize_text  # noqa: E402

import openminion  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
BASELINE = Path(sys.argv[3]).resolve() if len(sys.argv) > 3 else None
# Linux runner only: the job's PostgreSQL 16 (a success control outside the checklist).
PG_URL = os.environ.get("OPENMINION_TEST_POSTGRES_URL") or None
URL_RE = re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^\s\"']+")

CALL_MAIN = (
    "import json, sys\n"
    "from openminion.modules.storage.cli import main\n"
    "side = sys.argv[1]\n"
    "def record(**kw):\n"
    "    with open(side, 'w', encoding='utf-8') as fh:\n"
    "        json.dump(kw, fh)\n"
    "try:\n"
    "    rc = main(sys.argv[2:])\n"
    "except SystemExit as exc:\n"
    "    record(kind='raised', type='SystemExit', code=exc.code, message=str(exc))\n"
    "    raise\n"
    "except BaseException as exc:\n"
    "    record(kind='raised', type=type(exc).__name__, message=str(exc))\n"
    "    raise\n"
    "record(kind='returned', value=rc)\n"
)

WHERE = (
    "import importlib.util, json, os, sys\n"
    "import openminion\n"
    "spec = lambda n: importlib.util.find_spec(n).origin\n"
    "with_pkg = [p for p in sys.path if p and os.path.isfile(os.path.join(p, 'openminion', '__init__.py'))]\n"
    "print(json.dumps({'openminion.__file__': openminion.__file__,\n"
    "    'openminion.__version__': openminion.__version__,\n"
    "    'openminion.modules.storage.__main__': spec('openminion.modules.storage.__main__'),\n"
    "    'openminion.modules.storage.cli': spec('openminion.modules.storage.cli'),\n"
    "    'sys.path entries holding an openminion package': with_pkg,\n"
    "    'isolated': bool(sys.flags.isolated), 'cwd': os.getcwd(),\n"
    "    'cwd entries': sorted(os.listdir('.'))}))\n"
)

POOL_STATS_JSON = {
    "ok": False,
    "error": "pool-stats requires a Postgres URL. Pass --postgres-url or set "
    "OPENMINION_STORAGE_POSTGRES_URL. SQLite backends have no pool.",
}
A2A_ZERO_JSON = {"ok": False, "error": "a2a-archive-old requires --older-than-days >= 1."}
DROP_ENV = (
    "OPENMINION_STORAGE_POSTGRES_URL",
    "OPENMINION_TEST_POSTGRES_URL",
    "OPENMINION_GENERATED_ROOT",
    "OPENMINION_CONFIG_PATH",
)
KINDS = {
    "main()": ["-c", CALL_MAIN],
    "module": ["-m", "openminion.modules.storage"],
    "cli": ["-m", "openminion.modules.storage.cli"],
}


def cases() -> list[dict]:
    """name, args (relative to the case dir C), the files the case starts from, expected exit."""
    return [
        {"name": "plan --namespace session (success)", "checklist": True, "expect": 0,
         "args": lambda c: ["plan", "--namespace", "session", "--sqlite", str(c / "session.db")]},
        {"name": "pool-stats without a PostgreSQL URL", "checklist": True, "expect": 2, "json": POOL_STATS_JSON,
         "args": lambda c: ["pool-stats"]},
        {"name": "a2a-archive-old --older-than-days 0", "checklist": True, "expect": 2, "json": A2A_ZERO_JSON,
         "args": lambda c: ["a2a-archive-old", "--older-than-days", "0"]},
        {"name": "plan --namespace unknown-module (raises ValueError)", "checklist": True, "expect": 1,
         "args": lambda c: ["plan", "--namespace", "unknown-module", "--sqlite", str(c / "unknown.db")]},
        {"name": "a2a-archive-old --older-than-days 7 without a PostgreSQL URL", "expect": 2,
         "args": lambda c: ["a2a-archive-old", "--older-than-days", "7", "--audit-root", str(c / "audit")]},
        {"name": "export --backend postgres without a PostgreSQL URL", "expect": 2,
         "args": lambda c: ["export", "--namespace", "task", "--sqlite", str(c / "x.db"),
                            "--out", str(c / "omx-out"), "--backend", "postgres"]},
        {"name": "import --backend postgres without a PostgreSQL URL", "expect": 2, "setup": "omx-in",
         "args": lambda c: ["import", "--namespace", "task", "--sqlite", str(c / "x.db"),
                            "--input", str(c / "omx-in"), "--backend", "postgres"]},
        {"name": "dr-drill on a file that is not SQLite", "expect": 2, "setup": "not-sqlite",
         "args": lambda c: ["dr-drill", "--namespace", "session", "--sqlite", str(c / "not-sqlite.db")]},
        {"name": "restore from a missing snapshot (raises BackupError)", "expect": 1,
         "args": lambda c: ["restore", "--namespace", "session", "--sqlite", str(c / "r.db"),
                            "--snapshot-path", str(c / "missing-snapshot.db")]},
        {"name": "pool-stats --bogus (argparse usage error)", "expect": 2,
         "args": lambda c: ["pool-stats", "--bogus"]},
    ]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def variants(path: Path) -> list[str]:
    text = str(path)
    return sorted({text, text.replace("\\", "/"), text.replace("\\", "\\\\")}, key=len, reverse=True)


def norm(text: str, case_dir: Path) -> str:
    """CRLF -> LF, any PostgreSQL URL -> <PGURL>, the case dir -> <RUN_WORK>, then the shared sanitizer."""
    text = URL_RE.sub("<PGURL>", text.replace("\r\n", "\n"))
    for variant in variants(case_dir):
        text = re.sub(re.escape(variant), "<RUN_WORK>", text, flags=re.IGNORECASE)
    return sanitize_text(text)


TB_HEAD = "Traceback (most recent call last):"
MAIN_FRAME = re.compile(r'^\s*File ".*[\\/]openminion[\\/]modules[\\/]storage[\\/]cli\.py", line \d+, in main$')


def split_tb(stderr: str) -> dict:
    """Lines before the first traceback, the entry-point frames, and the rest
    (from storagectl's main() frame on). Without a traceback: all in `before`."""
    lines = stderr.split("\n")
    if TB_HEAD not in lines:
        return {"before": stderr, "entry": "", "from_main": ""}
    start = lines.index(TB_HEAD)
    at = next((i for i in range(start + 1, len(lines)) if MAIN_FRAME.match(lines[i])), len(lines))
    return {
        "before": "\n".join(lines[:start]),
        "entry": "\n".join(lines[start:at]),
        "from_main": "\n".join(lines[at:]),
    }


def tree(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(p.relative_to(root).as_posix() + ("/" if p.is_dir() else "") for p in root.rglob("*"))


def prepare(case_dir: Path, setup: str | None) -> None:
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True)
    if setup == "omx-in":
        (case_dir / "omx-in").mkdir()
    elif setup == "not-sqlite":
        (case_dir / "not-sqlite.db").write_bytes(b"this is not a SQLite database\n")


def main_status(side: dict | None) -> int | None:
    if side is None:
        return None
    if side["kind"] == "returned":
        return side["value"]
    if side["type"] == "SystemExit":
        code = side.get("code")
        return 0 if code is None else (code if isinstance(code, int) else 1)
    return 1


def record_digest(dist: metadata.Distribution, rel: str) -> str | None:
    record = dist.read_text("RECORD") or ""
    for row in csv.reader(io.StringIO(record)):
        if row and row[0] == rel and row[1]:
            return row[1]
    return None


def file_digest(path: Path, algo: str) -> str:
    raw = hashlib.new(algo, path.read_bytes()).digest()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def run_case(tag: str, args: list[str], case_dir: Path, setup: str | None, env: dict, base: list[str]) -> dict:
    """One child per entry point, each on fresh copies of the case files and in its own new empty cwd."""
    runs = {}
    for kind, prefix in KINDS.items():
        prepare(case_dir, setup)
        cwd = WORK / "cwd" / f"{tag}-{kind.strip('()')}"
        cwd.mkdir(parents=True)
        side = WORK / "side" / f"{tag}.json"
        side.parent.mkdir(exist_ok=True)
        side.unlink(missing_ok=True)
        extra = [str(side)] if kind == "main()" else []
        proc = subprocess.run(base + prefix + extra + args, cwd=cwd, env=env, capture_output=True, timeout=300,
                              check=False)
        run = {
            "exit": proc.returncode,
            "stdout": norm(proc.stdout.decode("utf-8", "replace"), case_dir),
            "stderr": norm(proc.stderr.decode("utf-8", "replace"), case_dir),
            "stdout_sha256": sha256(proc.stdout),
            "stderr_sha256": sha256(proc.stderr),
            "files_left": tree(case_dir) + [f"(cwd) {p}" for p in tree(cwd)],
        }
        run["stderr_parts"] = split_tb(run["stderr"])
        if kind == "main()":
            raw = json.loads(side.read_text(encoding="utf-8")) if side.exists() else None
            run["main_result"] = None if raw is None else {
                k: norm(v, case_dir) if isinstance(v, str) else v for k, v in raw.items()}
            run["main_status"] = main_status(run["main_result"])
            del run["exit"]  # the -c child only records main(); its own exit is not compared
        runs[kind] = run
    return runs


def main() -> int:
    checks = Checks()
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    dist = metadata.distribution("openminion")
    site = Path(dist.locate_file("")).resolve()
    pkg = Path(openminion.__file__).resolve().parent
    storage_dir = pkg / "modules" / "storage"
    main_py, cli_py = storage_dir / "__main__.py", storage_dir / "cli.py"
    cli_tail = [line for line in cli_py.read_text(encoding="utf-8").splitlines() if line.strip()][-2:]
    scripts = [ep for ep in dist.entry_points if ep.group == "console_scripts"]
    storage_scripts = [f"{ep.name} = {ep.value}" for ep in scripts if ep.value.startswith("openminion.modules.storage")]
    direct_url = dist.read_text("direct_url.json")
    info = banner(
        "probe_450",
        **{
            "distribution version": dist.version,
            "distribution location": str(site),
            "INSTALLER": (dist.read_text("INSTALLER") or "").strip(),
            "direct_url.json": direct_url,
            "sys.prefix": sys.prefix,
            "modules/storage/__main__.py sha256": sha256(main_py.read_bytes()),
            "modules/storage/__main__.py": main_py.read_text(encoding="utf-8"),
            "modules/storage/cli.py sha256": sha256(cli_py.read_bytes()),
            "modules/storage/cli.py last two lines": cli_tail,
            "console scripts (count)": len(scripts),
            "console scripts pointing into openminion.modules.storage": storage_scripts,
            "baseline": None if BASELINE is None else {
                "file": f"{BASELINE.parent.name}/{BASELINE.name}", "sha256": sha256(BASELINE.read_bytes())},
        },
    )

    # ------------------------------------------------------------ 1. package
    in_venv = Path(openminion.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    checks.check(
        "1. openminion is imported from this venv's site-packages (the installed distribution, not a checkout or editable install)",
        in_venv
        and "site-packages" in Path(openminion.__file__).as_posix()
        and Path(dist.locate_file("openminion/__init__.py")).resolve() == Path(openminion.__file__).resolve()
        and openminion.__version__ == dist.version
        and not (json.loads(direct_url).get("dir_info", {}).get("editable") if direct_url else False),
        observed={"openminion.__file__": openminion.__file__, "__version__": openminion.__version__,
                  "distribution version": dist.version, "INSTALLER": (dist.read_text("INSTALLER") or "").strip(),
                  "direct_url.json": direct_url, "python": sys.version.split()[0]},
    )
    rows = {}
    for rel_file in (main_py, cli_py):
        rel = rel_file.relative_to(site).as_posix()
        digest = record_digest(dist, rel)
        algo, _, expected = (digest or "").partition("=")
        rows[rel] = {"RECORD": digest, "installed": f"{algo}={file_digest(rel_file, algo)}" if algo else None}
    checks.check(
        "1. the installed modules/storage/__main__.py and cli.py match the wheel's RECORD",
        all(r["RECORD"] and r["RECORD"] == r["installed"] for r in rows.values()),
        observed=rows,
    )
    where_cwd = WORK / "where-cwd"
    where_cwd.mkdir()
    env = dict(os.environ)
    for key in DROP_ENV:
        env.pop(key, None)
    env["OPENMINION_HOME"] = str(WORK / "home")
    env["OPENMINION_DATA_ROOT"] = str(WORK / "home" / ".openminion")
    base = [sys.executable, "-I", "-X", "utf8"]
    done = subprocess.run(base + ["-c", WHERE], cwd=where_cwd, env=env, capture_output=True, timeout=300, check=False)
    try:
        where = json.loads(done.stdout.decode("utf-8"))
    except ValueError:
        where = {"exit": done.returncode, "stdout": done.stdout.decode("utf-8", "replace"),
                 "stderr": done.stderr.decode("utf-8", "replace")}
    same = lambda a, b: a is not None and Path(a).resolve() == Path(b).resolve()  # noqa: E731
    checks.check(
        "1. a child started like the commands below (python -I -X utf8, empty working directory) imports the same installed files; the only openminion package on its sys.path is this site-packages",
        same(where.get("openminion.__file__"), openminion.__file__)
        and same(where.get("openminion.modules.storage.__main__"), main_py)
        and same(where.get("openminion.modules.storage.cli"), cli_py)
        and [Path(p).resolve() for p in where.get("sys.path entries holding an openminion package", [])] == [site]
        and where.get("isolated") is True
        and where.get("cwd entries") == [],
        observed=where,
    )
    checks.check(
        "1. no storagectl console script in the package; the existing CLI entry point is `python -m openminion.modules.storage.cli` (cli.py ends with `raise SystemExit(main())`)",
        storage_scripts == [] and [s.strip() for s in cli_tail] == ['if __name__ == "__main__":', "raise SystemExit(main())"],
        observed={"console scripts pointing into openminion.modules.storage": storage_scripts,
                  "cli.py last two lines": cli_tail},
    )

    # ------------------------------------------------------------ run the cases
    results = []
    for index, case in enumerate(cases(), start=1):
        case_dir = WORK / f"c{index}"
        runs = run_case(str(index), case["args"](case_dir), case_dir, case.get("setup"), env, base)
        results.append({"case": case["name"], "checklist": bool(case.get("checklist")), "expect": case["expect"],
                        "args": [norm(a, case_dir) for a in case["args"](case_dir)], "runs": runs})
        m, c, d = runs["module"], runs["cli"], runs["main()"]
        print(f"[{index}] {case['name']}: main() {d['main_result']} | module exit {m['exit']} | cli exit {c['exit']}",
              flush=True)

    # ------------------------------------------------------------ 2-4. checklist cases
    by_name = {r["case"]: r for r in results}
    plan = by_name["plan --namespace session (success)"]["runs"]
    try:
        plan_json = json.loads(plan["module"]["stdout"])
    except ValueError:
        plan_json = None
    checks.check(
        "2. python -m openminion.modules.storage plan --namespace session --sqlite <db>: exit 0, stdout JSON ok true, main() returns 0",
        plan["module"]["exit"] == 0 and isinstance(plan_json, dict) and plan_json.get("ok") is True
        and plan["main()"]["main_result"] == {"kind": "returned", "value": 0},
        observed={"exit": plan["module"]["exit"], "stdout": plan["module"]["stdout"],
                  "stderr": plan["module"]["stderr"], "main()": plan["main()"]["main_result"]},
    )
    for name in ("pool-stats without a PostgreSQL URL", "a2a-archive-old --older-than-days 0"):
        res = by_name[name]
        runs = res["runs"]
        expected_json = next(c["json"] for c in cases() if c["name"] == name)
        try:
            got = json.loads(runs["module"]["stdout"])
        except ValueError:
            got = None
        checks.check(
            f"3. python -m openminion.modules.storage {' '.join(res['args'])}: exit 2 with the existing JSON output "
            "(exactly {ok: false, error: ...} as main() prints it; empty stderr); main() returns 2",
            runs["module"]["exit"] == 2 and got == expected_json
            and runs["module"]["stdout_sha256"] == runs["main()"]["stdout_sha256"]
            and runs["module"]["stderr"] == "" and runs["main()"]["main_result"] == {"kind": "returned", "value": 2},
            observed={"exit": runs["module"]["exit"], "stdout": runs["module"]["stdout"],
                      "stderr": runs["module"]["stderr"], "main()": runs["main()"]["main_result"]},
            expected={"exit": 2, "stdout JSON": expected_json},
        )
    unk = by_name["plan --namespace unknown-module (raises ValueError)"]["runs"]
    raised = unk["main()"]["main_result"] or {}
    last = (unk["module"]["stderr"].rstrip().splitlines() or [""])[-1]
    checks.check(
        "4. python -m openminion.modules.storage plan --namespace unknown-module: exit 1, empty stdout, stderr is the traceback ending in `ValueError: <the message main() raised>`",
        unk["module"]["exit"] == 1 and unk["module"]["stdout"] == "" and TB_HEAD in unk["module"]["stderr"]
        and raised.get("type") == "ValueError" and "unknown-module" in raised.get("message", "")
        and last == f"ValueError: {raised.get('message')}",
        observed={"exit": unk["module"]["exit"], "stdout": unk["module"]["stdout"],
                  "stderr last line": last, "main() raised": raised,
                  "entry-point frames": unk["module"]["stderr_parts"]["entry"]},
    )

    # ------------------------------------------------------------ 5. compare with the cli entry point
    failing = {}
    for index, res in enumerate(results, start=1):
        runs = res["runs"]
        m, c, d = runs["module"], runs["cli"], runs["main()"]
        status = d["main_status"]
        mp, cp, dp = m["stderr_parts"], c["stderr_parts"], d["stderr_parts"]
        raising = bool(mp["entry"])
        if raising:
            stderr_same = (mp["before"] == cp["before"] == dp["before"]
                           and mp["from_main"] == cp["from_main"] == dp["from_main"] != "")
        else:
            stderr_same = m["stderr_sha256"] == c["stderr_sha256"] == d["stderr_sha256"]
        tag = "" if res["checklist"] else " (extra case from the issue)"
        checks.check(
            f"5. [{index}] {res['case']}{tag}: module and cli exit {res['expect']}, the same status as main(); "
            "same stdout bytes as each other and main(); same stderr"
            + (" (same traceback from storagectl's main() frame on)" if raising else "")
            + "; same files left",
            m["exit"] == c["exit"] == status == res["expect"]
            and m["stdout_sha256"] == c["stdout_sha256"] == d["stdout_sha256"]
            and stderr_same and m["files_left"] == c["files_left"],
            observed={"main()": d["main_result"], "module exit": m["exit"], "cli exit": c["exit"],
                      "stdout identical (module, cli, main())": m["stdout_sha256"] == c["stdout_sha256"] == d["stdout_sha256"],
                      "stderr identical" + (" from main() frame" if raising else ""): stderr_same,
                      "stderr last line": (m["stderr"].rstrip().splitlines() or [""])[-1],
                      "files left (module)": m["files_left"], "files left (cli)": c["files_left"]},
        )
        if res["expect"] != 0:
            failing[res["case"]] = {"module": m["exit"], "cli": c["exit"]}
    checks.check(
        "5. every failing case exits nonzero from both python -m openminion.modules.storage and python -m openminion.modules.storage.cli",
        all(v["module"] != 0 and v["cli"] != 0 for v in failing.values()),
        observed=failing,
    )

    # ------------------------------------------------------------ PostgreSQL success control (runner only)
    pg = None
    if PG_URL:
        case_dir = WORK / "pg"
        pg = run_case("pg", ["pool-stats", "--postgres-url", PG_URL], case_dir, None, env, base)
        parsed = {}
        for kind in KINDS:
            try:
                parsed[kind] = json.loads(pg[kind]["stdout"])
            except ValueError:
                parsed[kind] = None
        print(f"[pg] pool-stats --postgres-url <PGURL>: main() {pg['main()']['main_result']} | "
              f"module exit {pg['module']['exit']} | cli exit {pg['cli']['exit']}", flush=True)
        checks.check(
            "PostgreSQL control (outside the checklist): pool-stats --postgres-url <the job's PostgreSQL>: "
            "main() returns 0; module and cli exit 0 and print ok true with the pool stats",
            pg["main()"]["main_result"] == {"kind": "returned", "value": 0}
            and pg["module"]["exit"] == 0 and pg["cli"]["exit"] == 0
            and all(isinstance(v, dict) and v.get("ok") is True and isinstance(v.get("pool"), dict)
                    for v in parsed.values()),
            observed={"main()": pg["main()"]["main_result"], "module exit": pg["module"]["exit"],
                      "cli exit": pg["cli"]["exit"], "module stdout": pg["module"]["stdout"],
                      "stdout identical (module, cli, main())":
                          pg["module"]["stdout_sha256"] == pg["cli"]["stdout_sha256"] == pg["main()"]["stdout_sha256"],
                      "module stderr": pg["module"]["stderr"]},
        )

    # ------------------------------------------------------------ baseline (0.1.13) comparison
    if BASELINE is not None:
        base_doc = json.loads(BASELINE.read_text(encoding="utf-8"))
        base_by_name = {r["case"]: r for r in base_doc["results"]}
        stdout_cmp, stderr_cmp, transitions = {}, {}, {}
        for res in results:
            old = base_by_name.get(res["case"], {}).get("runs", {}).get("module")
            new = res["runs"]["module"]
            if old is None:
                stdout_cmp[res["case"]] = stderr_cmp[res["case"]] = "missing in baseline"
                continue
            stdout_cmp[res["case"]] = old["stdout"] == new["stdout"]
            if new["stderr_parts"]["entry"] or old["stderr_parts"]["entry"]:
                stderr_cmp[res["case"]] = (old["stderr_parts"]["before"] == new["stderr_parts"]["before"]
                                           and old["stderr_parts"]["from_main"] == new["stderr_parts"]["from_main"])
                stderr_cmp[res["case"] + " (entry-point frames, baseline -> this run)"] = [
                    old["stderr_parts"]["entry"], new["stderr_parts"]["entry"]]
            else:
                stderr_cmp[res["case"]] = old["stderr"] == new["stderr"]
            transitions[res["case"]] = f"{old['exit']} -> {new['exit']}"
        base_version = base_doc.get("info", {}).get("distribution version")
        checks.check(
            f"vs {base_version}: python -m openminion.modules.storage prints the same stdout as on {base_version} in every case",
            all(v is True for v in stdout_cmp.values()),
            observed={"stdout identical": stdout_cmp, "module exit (baseline -> this run)": transitions},
        )
        checks.check(
            f"vs {base_version}: the same stderr as on {base_version} (raising commands: the same lines before the traceback and the same traceback from storagectl's main() frame on)",
            all(v is True for k, v in stderr_cmp.items() if not k.endswith("this run)")),
            observed=stderr_cmp,
        )

    summary = checks.summary()
    payload = {"info": info, "where (child)": where, "results": results, "postgres control": pg,
               "checks": checks.items, "summary": summary}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(sanitize(payload), indent=2, default=str) + "\n", encoding="utf-8", newline="\n")
    print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
    return 0 if checks.all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
