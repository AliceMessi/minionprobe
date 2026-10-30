"""List focus-pty-macos job conclusions across recent openminion ci.yml runs.

Usage: python -I ci_history.py <n_runs> <out.tsv>
"""
import json
import subprocess
import sys

N = int(sys.argv[1])
OUT = sys.argv[2]


def gh_json(*args):
    r = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise SystemExit(r.stderr)
    return json.loads(r.stdout)


runs = []
page = 1
while len(runs) < N:
    data = gh_json("api", f"repos/openminion/openminion/actions/workflows/ci.yml/runs?per_page=100&page={page}")
    batch = data.get("workflow_runs", [])
    if not batch:
        break
    runs.extend(batch)
    page += 1
runs = runs[:N]

rows = []
for run in runs:
    jobs = gh_json("api", f"repos/openminion/openminion/actions/runs/{run['id']}/jobs?per_page=100&filter=all")
    for job in jobs.get("jobs", []):
        if job.get("name") != "focus-pty-macos":
            continue
        rows.append(
            [
                str(run["id"]),
                str(job["id"]),
                str(run.get("run_attempt")),
                run.get("event") or "",
                (run.get("head_branch") or "")[:50],
                (run.get("head_sha") or "")[:8],
                run.get("created_at") or "",
                job.get("conclusion") or job.get("status") or "",
                ",".join(job.get("labels") or []),
                job.get("runner_name") or "",
            ]
        )

with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
    fh.write("run_id\tjob_id\tattempt\tevent\tbranch\tsha\tcreated\tconclusion\tlabels\trunner\n")
    for row in rows:
        fh.write("\t".join(row) + "\n")
counts = {}
for row in rows:
    counts[row[7]] = counts.get(row[7], 0) + 1
print("runs scanned:", len(runs), "focus-pty-macos jobs:", len(rows), counts)
for row in rows:
    if row[7] not in ("success", "skipped"):
        print("\t".join(row))
