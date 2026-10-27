"""Check that the published 0.1.13 release source contains each fix.

    python -I release_source.py <git_dir> <tag> <sdist.tar.gz> <wheel.whl> <prs.json> <out.json>

<git_dir> is a clone of openminion/openminion with the release tag fetched.
<prs.json> maps issue -> {pr, merge_commit_sha, head_sha, files[]} (from the
GitHub pulls API). For every fix it records:

1. whether the PR's merge commit and reviewed head commit are ancestors of the
   tag commit (`git merge-base --is-ancestor`);
2. for every file the PR touched that exists at the tag: whether the sdist
   member and (for src/openminion/...) the wheel member are byte-identical to
   the file at the tag;
3. whether the PR's own version of each touched file (at its head commit) is
   unchanged at the tag, or was changed afterwards (later merges such as the
   CHANGELOG reconciles); changed files are listed, not judged.

It also compares every `openminion/` file in the wheel and every file in the
sdist with the tag tree. The archives are only read, never imported.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

GIT, TAG, SDIST, WHEEL, PRS, OUT = sys.argv[1:7]


def git(*args: str, binary: bool = False):
    proc = subprocess.run(["git", "-C", GIT, *args], capture_output=True, check=False)
    return proc if binary else proc


def blob(rev: str, path: str) -> bytes | None:
    proc = subprocess.run(["git", "-C", GIT, "show", f"{rev}:{path}"], capture_output=True, check=False)
    return proc.stdout if proc.returncode == 0 else None


def sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


tag_commit = subprocess.run(["git", "-C", GIT, "rev-parse", f"{TAG}^{{commit}}"], capture_output=True, text=True).stdout.strip()
tag_object = subprocess.run(["git", "-C", GIT, "rev-parse", TAG], capture_output=True, text=True).stdout.strip()
tag_files = set(
    subprocess.run(["git", "-C", GIT, "ls-tree", "-r", "--name-only", tag_commit], capture_output=True, text=True).stdout.splitlines()
)

with tarfile.open(SDIST, "r:gz") as tf:
    members = {m.name: m for m in tf.getmembers() if m.isfile()}
    prefix = sorted({n.split("/", 1)[0] for n in members})[0] + "/"
    sdist = {n[len(prefix):]: tf.extractfile(m).read() for n, m in members.items()}  # type: ignore[union-attr]
with zipfile.ZipFile(WHEEL) as zf:
    wheel = {n: zf.read(n) for n in zf.namelist() if not n.endswith("/")}


def wheel_name(path: str) -> str | None:
    return path[len("src/"):] if path.startswith("src/openminion/") else None


# Whole-archive comparison with the tag tree.
sdist_cmp = {"identical": 0, "different": [], "not_in_tag": []}
for rel, data in sorted(sdist.items()):
    if rel not in tag_files:
        sdist_cmp["not_in_tag"].append(rel)
        continue
    if blob(tag_commit, rel) == data:
        sdist_cmp["identical"] += 1
    else:
        sdist_cmp["different"].append(rel)
wheel_cmp = {"identical": 0, "different": [], "not_in_tag": []}
for name, data in sorted(wheel.items()):
    if not name.startswith("openminion/"):
        continue
    rel = "src/" + name
    if rel not in tag_files:
        wheel_cmp["not_in_tag"].append(name)
        continue
    if blob(tag_commit, rel) == data:
        wheel_cmp["identical"] += 1
    else:
        wheel_cmp["different"].append(name)

prs = json.loads(Path(PRS).read_text(encoding="utf-8"))
fixes = {}
for issue, info in prs.items():
    anc = {}
    for label in ("merge_commit_sha", "head_sha"):
        rev = info[label]
        rc = subprocess.run(["git", "-C", GIT, "merge-base", "--is-ancestor", rev, tag_commit], capture_output=True).returncode
        anc[label] = {"sha": rev, "ancestor_of_tag": rc == 0}
    files = []
    for f in info["files"]:
        path = f["filename"]
        at_tag = blob(tag_commit, path)
        at_head = blob(info["head_sha"], path)
        entry = {"path": path, "pr_status": f["status"], "in_tag": at_tag is not None,
                 "tag_sha256": sha(at_tag), "pr_head_equals_tag": at_head == at_tag}
        if at_tag is not None:
            entry["in_sdist"] = path in sdist
            entry["sdist_equals_tag"] = sdist.get(path) == at_tag if path in sdist else None
            wn = wheel_name(path)
            if wn is not None:
                entry["in_wheel"] = wn in wheel
                entry["wheel_equals_tag"] = wheel.get(wn) == at_tag if wn in wheel else None
        files.append(entry)
    src_files = [e for e in files if e["path"].startswith("src/") and e["in_tag"]]
    in_wheel = [e for e in src_files if e.get("in_wheel")]
    in_sdist = [e for e in src_files if e.get("in_sdist")]
    fixes[issue] = {
        "pr": info["pr"],
        "commits": anc,
        "touched_files": len(files),
        "touched_src_files": len(src_files),
        "src_files_in_wheel": len(in_wheel),
        "src_files_wheel_equal_tag": all(e["wheel_equals_tag"] for e in in_wheel),
        "src_files_in_sdist": len(in_sdist),
        "src_files_sdist_equal_tag": all(e["sdist_equals_tag"] for e in in_sdist),
        "src_files_not_packaged": [e["path"] for e in src_files if not e.get("in_wheel") and not e.get("in_sdist")],
        "files_changed_after_pr_head": [e["path"] for e in files if e["in_tag"] and not e["pr_head_equals_tag"]],
        "files": files,
    }

result = {
    "tag": TAG,
    "tag_object": tag_object,
    "tag_commit": tag_commit,
    "sdist": Path(SDIST).name,
    "sdist_sha256": sha(Path(SDIST).read_bytes()),
    "wheel": Path(WHEEL).name,
    "wheel_sha256": sha(Path(WHEEL).read_bytes()),
    "sdist_vs_tag": {**sdist_cmp, "files": len(sdist)},
    "wheel_openminion_vs_tag": {**wheel_cmp, "files": sum(1 for n in wheel if n.startswith("openminion/"))},
    "all_fix_commits_in_tag": all(c["ancestor_of_tag"] for f in fixes.values() for c in f["commits"].values()),
    "fixes": fixes,
}
Path(OUT).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print(json.dumps({k: v for k, v in result.items() if k != "fixes"}, indent=2))
for issue, f in fixes.items():
    print(issue, f"PR #{f['pr']}", {k: v["ancestor_of_tag"] for k, v in f["commits"].items()},
          f"src files {f['touched_src_files']} (wheel {f['src_files_in_wheel']}, sdist {f['src_files_in_sdist']})",
          "wheel==tag", f["src_files_wheel_equal_tag"], "sdist==tag", f["src_files_sdist_equal_tag"],
          "not packaged:", f["src_files_not_packaged"], "changed after head:", f["files_changed_after_pr_head"])
