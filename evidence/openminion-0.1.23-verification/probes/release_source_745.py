"""#745 published-source check for openminion 0.1.23.

Usage: python -I release_source_745.py GIT_DIR TAG DL_DIR
Compares the PyPI wheel, sdist and project description with the tag, and checks
that the #747 merge and its reviewed head are ancestors of the tag.
Exit 0 only if every check passes.
"""
import hashlib, io, json, subprocess, sys, tarfile, zipfile
from pathlib import Path

git_dir, tag, dl = sys.argv[1], sys.argv[2], Path(sys.argv[3])
MERGE, HEAD = "443d5e33ea86ec8b572fbbe8f4cba44709182a9d", "f2a3eb29ea323c5371dba39b09c7d7162d50a2c4"
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
for label, sha in (("#747 merge", MERGE), ("#747 reviewed head", HEAD)):
    ok = subprocess.run(["git", "-C", git_dir, "merge-base", "--is-ancestor", sha, tag]).returncode == 0
    check(f"{label} {sha[:8]} is an ancestor of {tag}", ok)

meta = json.loads((dl / "pypi-0.1.23.json").read_text(encoding="utf-8"))
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
    dec = z.read("openminion/tools/decorator.py")
check("wheel openminion/tools/decorator.py == tag", dec == tag_blob("src/openminion/tools/decorator.py"),
      hashlib.sha256(dec).hexdigest())

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
readme_tag = tag_blob("README.md")
check("sdist README.md == tag README.md", readme_sdist == readme_tag, hashlib.sha256(readme_tag).hexdigest())
desc = meta["info"]["description"].encode("utf-8")
check("PyPI project description == tag README.md", desc == readme_tag,
      f"description sha256 {hashlib.sha256(desc).hexdigest()}")
for path in ("README.md", "docs/runtime-surfaces.md", "examples/sdk/tool_agent.py", "src/openminion/tools/decorator.py"):
    print(f"blob {tag}:{path} = {git('rev-parse', f'{tag}:{path}')}")
ok = all(r["pass"] for r in results)
print(f"SUMMARY passed={sum(r['pass'] for r in results)} failed={sum(not r['pass'] for r in results)} total={len(results)}")
sys.exit(0 if ok else 1)
