"""#717 published-source check for openminion 0.1.24.

Usage: python -I release_source_717.py GIT_DIR TAG DL_DIR
Compares the PyPI wheel and sdist with the tag, checks that the #795 merge and
its reviewed head are ancestors of the tag and not of v0.1.23 (the control),
and that the #795 runtime files and CHANGELOG entry are in the release.
Exit 0 only if every check passes.
"""
import hashlib, io, json, subprocess, sys, tarfile, zipfile
from pathlib import Path

git_dir, tag, dl = sys.argv[1], sys.argv[2], Path(sys.argv[3])
MERGE, HEAD = "4204bbf024d9e85a126e0b563035890f87cf3fbd", "6c34523134722312583c85919193717d65e1c672"
CONTROL = "v0.1.23"
FILES = ("openminion/modules/llm/runtime/flow.py", "openminion/modules/llm/providers/transport/error_facts.py",
         "openminion/modules/brain/loop/orchestration.py")
results = []

def check(name, ok, detail=""):
    results.append({"check": name, "pass": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))

def git(*a, binary=False):
    out = subprocess.run(["git", "-C", git_dir, *a], check=True, capture_output=True).stdout
    return out if binary else out.decode("utf-8").strip()

def tag_blob(path):
    return git("show", f"{tag}:{path}", binary=True)

commit = git("rev-parse", f"{tag}^{{commit}}")
check("tag resolves", True, f"{tag} -> tag object {git('rev-parse', tag)}, commit {commit}")
for label, sha in (("#795 merge", MERGE), ("#795 reviewed head", HEAD)):
    ok = subprocess.run(["git", "-C", git_dir, "merge-base", "--is-ancestor", sha, tag]).returncode == 0
    check(f"{label} {sha[:8]} is an ancestor of {tag}", ok)
    ok = subprocess.run(["git", "-C", git_dir, "merge-base", "--is-ancestor", sha, CONTROL]).returncode != 0
    check(f"{label} {sha[:8]} is NOT an ancestor of {CONTROL} (the negative-control release)", ok)

meta = json.loads((dl / "pypi-0.1.24.json").read_text(encoding="utf-8"))
for u in meta["urls"]:
    data = (dl / u["filename"]).read_bytes()
    check(f"{u['filename']} sha256 matches PyPI", hashlib.sha256(data).hexdigest() == u["digests"]["sha256"], u["digests"]["sha256"])

whl = next(dl.glob("*.whl")); sdist = next(dl.glob("*.tar.gz"))
tag_files = set(git("ls-tree", "-r", "--name-only", tag).splitlines())
diff, n = [], 0
with zipfile.ZipFile(whl) as z:
    for name in z.namelist():
        if ".dist-info/" in name:
            continue
        n += 1
        src = "src/" + name
        if src not in tag_files or z.read(name) != tag_blob(src):
            diff.append(name)
check(f"wheel: all {n} package files byte-identical to {tag}:src/", not diff, ", ".join(diff[:5]))
with zipfile.ZipFile(whl) as z:
    for f in FILES:
        data = z.read(f)
        same_merge = data == git("show", f"{MERGE}:src/{f}", binary=True)
        check(f"wheel {f} == tag", data == tag_blob(f"src/{f}"),
              f"sha256 {hashlib.sha256(data).hexdigest()}; identical to the #795 merge's blob: {same_merge}")
    flow = z.read("openminion/modules/llm/runtime/flow.py").decode("utf-8")
    facts = z.read("openminion/modules/llm/providers/transport/error_facts.py").decode("utf-8")
check("wheel flow.py has the route admission scope and hinted wait; error_facts.py has retry_after_seconds",
      all(x in flow for x in ("def route_admission_scope", "def _hinted_wait_fits", "def _wait_until"))
      and "def retry_after_seconds" in facts)

diff, n, root, generated = [], 0, None, []
with tarfile.open(sdist) as t:
    for m in t.getmembers():
        if not m.isfile():
            continue
        root = m.name.split("/", 1)[0]
        rel = m.name.split("/", 1)[1]
        if rel == "PKG-INFO" or (rel not in tag_files and (rel == "setup.cfg" or rel.startswith("src/openminion.egg-info/"))):
            generated.append(rel)
            continue
        n += 1
        if rel not in tag_files or t.extractfile(m).read() != tag_blob(rel):
            diff.append(rel)
    readme_sdist = t.extractfile(f"{root}/README.md").read()
check(f"sdist: all {n} source files byte-identical to {tag}", not diff, ", ".join(diff[:5]))
print("sdist build-generated files (not in the tag, not compared): " + ", ".join(sorted(generated)))
changelog = tag_blob("CHANGELOG.md").decode("utf-8")
entry = changelog.find("(Refs #717)")
section = changelog.rfind("\n## ", 0, entry)
heading = changelog[section + 1:changelog.find("\n", section + 1)] if entry >= 0 and section >= 0 else ""
check("tag CHANGELOG.md has the #717 Retry-After entry in a released section (not Unreleased)",
      entry >= 0 and heading and "Unreleased" not in heading, heading)
for path in ("CHANGELOG.md",) + tuple("src/" + f for f in FILES):
    print(f"blob {tag}:{path} = {git('rev-parse', f'{tag}:{path}')}")
ok = all(r["pass"] for r in results)
print(f"SUMMARY passed={sum(r['pass'] for r in results)} failed={sum(not r['pass'] for r in results)} total={len(results)}")
sys.exit(0 if ok else 1)
