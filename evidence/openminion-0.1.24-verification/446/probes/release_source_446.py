"""#446 published-source check for openminion 0.1.24 (PR #817).

Usage: python -I release_source_446.py GIT_DIR TAG DL_DIR [PREV_TAG]
Compares the PyPI wheel and sdist with the tag, checks that the #817 merge and its contributor
head are ancestors of the tag (and not of PREV_TAG, the control release), that the wheel's
postgres_schema.py equals the file at the #817 head, and records the blobs of the files #817
changed. Exit 0 only if every check passes.
"""
import hashlib, json, subprocess, sys, tarfile, zipfile
from pathlib import Path

git_dir, tag, dl = sys.argv[1], sys.argv[2], Path(sys.argv[3])
prev = sys.argv[4] if len(sys.argv) > 4 else None
MERGE, HEAD = "01e47b868ab065b90a44cde06fe31ef72f9c652a", "4cbc5e11a92021fef3f6a18d50be2efecf8129aa"
TREE = "424aa7dcd1e750506bdedbc1bb9450c7b1a1dd6b"
CHANGED = ("CHANGELOG.md", "docs/runtime-database-upgrades.md",
           "src/openminion/modules/storage/migrations/postgres_schema.py",
           "tests/integration/test_postgres_module_schemas.py", "tests/storage/test_postgres_schema.py")
SCHEMA = "src/openminion/modules/storage/migrations/postgres_schema.py"
results = []


def check(name, ok, detail=""):
    results.append({"check": name, "pass": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


def git(*a, binary=False):
    out = subprocess.run(["git", "-C", git_dir, *a], check=True, capture_output=True).stdout
    return out if binary else out.decode("utf-8").strip()


def ancestor(sha, ref):
    return subprocess.run(["git", "-C", git_dir, "merge-base", "--is-ancestor", sha, ref]).returncode == 0


def tag_blob(path):
    return git("show", f"{tag}:{path}", binary=True)


commit = git("rev-parse", f"{tag}^{{commit}}")
check("tag resolves", True, f"{tag} -> tag object {git('rev-parse', tag)}, commit {commit}")
check(f"#817 merge {MERGE[:8]} has tree {TREE[:8]}", git("rev-parse", f"{MERGE}^{{tree}}") == TREE)
parents = git("rev-list", "--parents", "-n", "1", MERGE).split()[1:]
check(f"#817 merge parents include the contributor head {HEAD[:8]}", HEAD in parents, " ".join(p[:8] for p in parents))
for label, sha in (("#817 merge", MERGE), ("#817 contributor head", HEAD)):
    check(f"{label} {sha[:8]} is an ancestor of {tag}", ancestor(sha, tag))
    if prev:
        check(f"{label} {sha[:8]} is not an ancestor of {prev} (the control)", not ancestor(sha, prev))
for path in CHANGED:
    same = git("rev-parse", f"{HEAD}:{path}") == git("rev-parse", f"{tag}:{path}")
    print(f"blob {path}: #817 head {git('rev-parse', f'{HEAD}:{path}')[:12]}, {tag} {git('rev-parse', f'{tag}:{path}')[:12]}"
          f"{'' if same else ' (changed after #817)'}")
log = git("log", "--format=%h %s", f"{HEAD}..{tag}", "--", SCHEMA)
check(f"{SCHEMA} unchanged between the #817 head and {tag}", git("rev-parse", f"{HEAD}:{SCHEMA}") ==
      git("rev-parse", f"{tag}:{SCHEMA}"), log or "no later commit")

ver = tag.lstrip("v")
meta = json.loads((dl / f"pypi-{ver}.json").read_text(encoding="utf-8"))
for u in meta["urls"]:
    data = (dl / u["filename"]).read_bytes()
    check(f"{u['filename']} sha256 matches PyPI ({u['upload_time_iso_8601']})",
          hashlib.sha256(data).hexdigest() == u["digests"]["sha256"], u["digests"]["sha256"])

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
    schema_whl = z.read("openminion/modules/storage/migrations/postgres_schema.py")
    hist_whl = z.read("openminion/modules/storage/migrations/postgres_histories.json")
check(f"wheel: all {n} package files byte-identical to {tag}:src/", not diff, ", ".join(diff[:5]))
check("wheel postgres_schema.py == the #817 contributor head's file",
      schema_whl == git("show", f"{HEAD}:{SCHEMA}", binary=True), hashlib.sha256(schema_whl).hexdigest())
print(f"wheel postgres_histories.json sha256 {hashlib.sha256(hist_whl).hexdigest()}")

diff, n, generated = [], 0, []
with tarfile.open(sdist) as t:
    for m in t.getmembers():
        if not m.isfile():
            continue
        rel = m.name.split("/", 1)[1]
        if rel == "PKG-INFO" or (rel not in tag_files and (rel == "setup.cfg" or rel.startswith("src/openminion.egg-info/"))):
            generated.append(rel)
            continue
        n += 1
        if rel not in tag_files or t.extractfile(m).read() != tag_blob(rel):
            diff.append(rel)
check(f"sdist: all {n} source files byte-identical to {tag}", not diff, ", ".join(diff[:5]))
print("sdist build-generated files (not in the tag, not compared): " + ", ".join(sorted(generated)))
ok = all(r["pass"] for r in results)
print(f"SUMMARY passed={sum(r['pass'] for r in results)} failed={sum(not r['pass'] for r in results)} total={len(results)}")
sys.exit(0 if ok else 1)
