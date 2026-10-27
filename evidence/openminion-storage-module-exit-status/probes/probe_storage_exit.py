"""Exit status of `python -m openminion.modules.storage` compared with storagectl's main().

Usage (from an empty directory, with the interpreter of the venv under test):

    python -I -X utf8 probe_storage_exit.py --label LABEL --work WORKDIR --out RESULT.json

For each case the probe starts three child processes with the same interpreter,
the same arguments, the same environment and an empty working directory:

    module : python -I -X utf8 -m openminion.modules.storage ARGS
    cli    : python -I -X utf8 -m openminion.modules.storage.cli ARGS
             (cli.py itself ends with `raise SystemExit(main())`)
    main() : python -I -X utf8 -c CALL_MAIN ARGS
             calls openminion.modules.storage.cli.main(ARGS) and records what it
             returned (or raised) in a side file, so its stdout/stderr stay as
             main() wrote them

It prints the exit statuses, main()'s result and whether the three stdouts and
the module/cli stderrs are identical. The child environment drops
OPENMINION_STORAGE_POSTGRES_URL and points OPENMINION_HOME / OPENMINION_DATA_ROOT
at WORKDIR. Paths in the printed and saved output are replaced by <WORK>,
<VENV> and <USER_HOME>.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

CALL_MAIN = (
    "import sys\n"
    "from openminion.modules.storage.cli import main\n"
    "side = sys.argv[1]\n"
    "try:\n"
    "    rc = main(sys.argv[2:])\n"
    "except SystemExit as exc:\n"
    "    open(side, 'w').write('raised SystemExit(%r)' % (exc.code,))\n"
    "    raise\n"
    "except BaseException as exc:\n"
    "    open(side, 'w').write('raised ' + type(exc).__name__)\n"
    "    raise\n"
    "open(side, 'w').write('returned %r' % (rc,))\n"
)


def _cases(work: Path) -> list[tuple[str, list[str]]]:
    w = work / "paths"
    return [
        ("plan (success control)", ["plan", "--namespace", "session", "--sqlite", str(w / "plan" / "session.db")]),
        ("pool-stats without a Postgres URL", ["pool-stats"]),
        ("a2a-archive-old --older-than-days 0", ["a2a-archive-old", "--older-than-days", "0", "--audit-root", str(w / "audit")]),
        ("a2a-archive-old without a Postgres URL", ["a2a-archive-old", "--older-than-days", "7", "--audit-root", str(w / "audit")]),
        ("export --backend postgres without a Postgres URL", ["export", "--namespace", "task", "--sqlite", str(w / "x.db"), "--out", str(w / "omx-out"), "--backend", "postgres"]),
        ("import --backend postgres without a Postgres URL", ["import", "--namespace", "task", "--sqlite", str(w / "x.db"), "--input", str(w / "omx-in"), "--backend", "postgres"]),
        ("dr-drill on a file that is not SQLite", ["dr-drill", "--namespace", "session", "--sqlite", str(w / "not-sqlite.db")]),
        ("plan with an unknown module (raises)", ["plan", "--namespace", "unknown-module", "--sqlite", str(w / "u.db")]),
        ("restore from a missing snapshot (raises)", ["restore", "--namespace", "session", "--sqlite", str(w / "r.db"), "--snapshot-path", str(w / "missing-snapshot.db")]),
        ("unknown option (argparse control)", ["pool-stats", "--bogus"]),
    ]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--out", required=True)
    opts = parser.parse_args()

    work = Path(opts.work).resolve()
    if work.exists():
        shutil.rmtree(work)
    (work / "cwd").mkdir(parents=True)
    (work / "home" / ".openminion").mkdir(parents=True)
    (work / "side").mkdir()
    paths = work / "paths"
    (paths / "omx-in").mkdir(parents=True)
    (paths / "not-sqlite.db").write_text("this is not a SQLite database\n", encoding="utf-8")

    venv = Path(sys.prefix).resolve()
    user_home = Path.home().resolve()
    replacements = []
    for raw, token in ((work, "<WORK>"), (venv, "<VENV>"), (user_home, "<USER_HOME>")):
        text = str(raw)
        for variant in (text.replace("\\", "\\\\"), text, text.replace("\\", "/")):
            replacements.append((variant, token))

    def clean(value: str) -> str:
        for old, new in replacements:
            value = value.replace(old, new)
        return value

    env = dict(os.environ)
    env.pop("OPENMINION_STORAGE_POSTGRES_URL", None)
    env["OPENMINION_HOME"] = str(work / "home")
    env["OPENMINION_DATA_ROOT"] = str(work / "home" / ".openminion")
    base = [sys.executable, "-I", "-X", "utf8"]

    import openminion
    import openminion.modules.storage.cli as cli_module

    pkg = Path(openminion.__file__).resolve().parent
    main_py = pkg / "modules" / "storage" / "__main__.py"
    from importlib.metadata import version

    header = {
        "label": opts.label,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "openminion": version("openminion"),
        "openminion_package": clean(str(pkg)),
        "storage___main___py_sha256": _sha256(main_py),
        "storage_cli_py_sha256": _sha256(Path(cli_module.__file__)),
        "storage___main___py": main_py.read_text(encoding="utf-8"),
    }
    print(f"# {opts.label}: openminion {header['openminion']}, Python {header['python']}, {header['platform']}")
    print(f"# package: {header['openminion_package']}")
    print(f"# modules/storage/__main__.py sha256 {header['storage___main___py_sha256']}:")
    for line in header["storage___main___py"].splitlines():
        print(f"#   {line}")

    results = []
    for index, (name, args) in enumerate(_cases(work), start=1):
        side = work / "side" / f"{index}.txt"
        runs = {}
        for kind, prefix in (
            ("main()", base + ["-c", CALL_MAIN, str(side)]),
            ("module", base + ["-m", "openminion.modules.storage"]),
            ("cli", base + ["-m", "openminion.modules.storage.cli"]),
        ):
            done = subprocess.run(
                prefix + args,
                cwd=work / "cwd",
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=300,
                check=False,
            )
            runs[kind] = {
                "exit": done.returncode,
                "stdout": clean(done.stdout),
                "stderr": clean(done.stderr),
            }
        main_result = side.read_text(encoding="utf-8") if side.exists() else "no record"
        stdout_same = runs["main()"]["stdout"] == runs["module"]["stdout"] == runs["cli"]["stdout"]
        stderr_same = runs["module"]["stderr"] == runs["cli"]["stderr"]
        last_err = [
            (runs[k]["stderr"].strip().splitlines() or [""])[-1] for k in ("main()", "module", "cli")
        ]
        record = {
            "case": name,
            "args": [clean(a) for a in args],
            "main_result": main_result,
            "exit_module": runs["module"]["exit"],
            "exit_cli": runs["cli"]["exit"],
            "stdout_identical": stdout_same,
            "stderr_identical_module_cli": stderr_same,
            "stderr_last_line": last_err,
            "runs": runs,
        }
        results.append(record)
        print()
        print(f"[{index}] {name}")
        print(f"    args: {' '.join(record['args'])}")
        print(f"    main(): {main_result}")
        print(f"    python -m openminion.modules.storage     -> exit {record['exit_module']}")
        print(f"    python -m openminion.modules.storage.cli -> exit {record['exit_cli']}")
        print(f"    stdout identical (main()/module/cli): {stdout_same}; stderr identical (module/cli): {stderr_same}")
        out_text = runs["module"]["stdout"].strip()
        if out_text:
            shown = out_text if len(out_text) <= 400 else out_text[:400] + " ..."
            print("    stdout: " + shown.replace("\n", "\n            "))
        if last_err[1]:
            print(f"    stderr last line: {last_err[1]}")

    mismatched = [r["case"] for r in results if r["exit_module"] != r["exit_cli"]]
    print()
    print(f"# cases where the module exit differs from cli.py's raise SystemExit(main()): {len(mismatched)} of {len(results)}")
    for case in mismatched:
        print(f"#   - {case}")
    Path(opts.out).write_text(
        json.dumps({"header": header, "results": results}, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
