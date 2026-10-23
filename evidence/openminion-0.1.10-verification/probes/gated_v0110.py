"""Run the #328 Gemini scenario on the installed 0.1.10, one turn at a time, only
while the endpoint answers 200. First attempt uses pickup code ZEBRA-42, then 43, 44...
Stops at 3 attempts with a judge HTTP 200, or 8 attempts, or the deadline."""

import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = str(ROOT / "venv" / "Scripts" / "python.exe")
OUT = ROOT / "live"
CWD = ROOT / "run-cwd-live"
LOGS = ROOT / "live-logs"
for d in (OUT, CWD, LOGS):
    d.mkdir(parents=True, exist_ok=True)
DEADLINE = time.time() + 150 * 60
MAX_ATTEMPTS = 8
TARGET_JUDGED = 3
LOG = ROOT / "live-gate.log"


def log(line: str) -> None:
    stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{stamp} {line}\n")
    print(f"{stamp} {line}", flush=True)


def ping() -> bool:
    proc = subprocess.run([PY, "-I", str(HERE / "gemini_ping.py")], check=False, capture_output=True, text=True, cwd=str(CWD))
    log(f"ping: {proc.stdout.strip() or proc.stderr.strip()[-200:]}")
    return proc.returncode == 0


judged = attempts = index = 0
while time.time() < DEADLINE and attempts < MAX_ATTEMPTS and judged < TARGET_JUDGED:
    if not ping():
        time.sleep(180)
        continue
    attempts += 1
    log(f"attempt {attempts}: start run index {index} (code ZEBRA-{42 + index})")
    with (LOGS / f"attempt{attempts}.log").open("w", encoding="utf-8") as fh:
        subprocess.run(
            [PY, "-I", str(HERE / "capture_328_v0110.py"), "--provider", "gemini", "--runs", "1",
             "--start-index", str(index), "--code-offset", "0", "--turn-timeout", "300",
             "--out", str(OUT), "--label", "v0110"],
            check=False, stdout=fh, stderr=subprocess.STDOUT, cwd=str(CWD),
            env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
        )
    record_path = OUT / f"v0110-run{index}.json"
    index += 1
    if not record_path.exists():
        log(f"attempt {attempts}: no record written")
        continue
    record = json.loads(record_path.read_text(encoding="utf-8"))
    statuses = [c["status"] for c in record["wire"]]
    judge_statuses = [c["status"] for c in record["wire"] if c["kind"] == "closure_judge"]
    if any(status == 200 for status in judge_statuses):
        judged += 1
    log(
        f"attempt {attempts}: {record_path.name} code={record['pickup_code']} http={statuses} judge={judge_statuses} "
        f"shapes={[(s['system_message_count'], s['system_has_tool_output'], s['tool_output_before_contract']) for s in record['judge_request_shapes']]} "
        f"result_has_code={record['result_text_contains_code']} error={record['error'][:120]!r} result={record['result_text'][:140]!r}"
    )
    time.sleep(30)
log(f"done: attempts={attempts} judged={judged}")
