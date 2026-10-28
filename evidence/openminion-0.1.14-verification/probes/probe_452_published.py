"""#452: where the corrected guidance is published for 0.1.14.

#453 changed only docs/runtime-database-upgrades.md, docs/README.md and
CHANGELOG.md, and none of them is in the wheel or the sdist. This probe records,
from the GitHub API, raw.githubusercontent.com and PyPI's JSON API:
  - the tag v0.1.14 (annotated tag -> commit) and the guidance file at the tag,
    the #453 head and merge, v0.1.13 and main (git blob id and SHA-256);
  - that the text at the tag carries the corrected guidance (section, table,
    backup and rollback steps, typed-save paragraph) and the v0.1.13 text does not;
  - docs/README.md's index entry, the CHANGELOG entry at the tag and on main;
  - the GitHub release v0.1.14 notes, and the compare v0.1.13...v0.1.14;
  - PyPI: the project description, the project URLs (and whether the
    Documentation site serves the page), and the wheel and sdist contents
    (downloaded into <work_dir>/dl, never executed or imported);
  - the installed package: no guidance file in its RECORD, and no installed file
    that names the guidance.

Usage: python -I -X utf8 probe_452_published.py <work_dir> <out.json>
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import shutil
import sys
import tarfile
import urllib.error
import urllib.request
import zipfile
from importlib import metadata
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, quiet, write_result  # noqa: E402
import probe_452_lib as L  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
quiet()
if WORK.exists():
    shutil.rmtree(WORK)
(WORK / "dl").mkdir(parents=True)
info = banner("probe_452_published")
c = Checks()
observed: dict[str, Any] = {}
API = "https://api.github.com/repos/" + L.REPO
RAW = "https://raw.githubusercontent.com/" + L.REPO
TAG_OBJECT, TAG_COMMIT = "a029066d46c2b8a2544f6308e9e277271620ed30", "6f2c8f4595e71de7dff9f010872f1733aa1b8b8c"
PR_HEAD, PR_MERGE = "2d8fd446f699c1c96345607fbcdb475c9ec85942", "65ad947b5c5c7d7f3b49b0d6afcc9325e18e5298"
PR_FILES = ("docs/runtime-database-upgrades.md", "docs/README.md", "CHANGELOG.md")


def api(path: str) -> Any:
    req = urllib.request.Request(API + path, headers={"Accept": "application/vnd.github+json",
                                                      "User-Agent": "minionprobe-452"})
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - fixed https URL
        return json.load(r)


def status_of(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "minionprobe-452"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - fixed https URL
            body = r.read()
            return {"status": r.status, "final_url": r.geturl(), "bytes": len(body), "body": body}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "final_url": url, "bytes": 0, "body": b""}


def flat(text: str) -> str:
    return " ".join(text.split())


# ======================================================== tag and the guidance file
ref = api("/git/ref/tags/v0.1.14")
tag = api(f"/git/tags/{ref['object']['sha']}") if ref["object"]["type"] == "tag" else {}
observed["tag"] = {"ref_object": ref["object"], "tag_object_target": tag.get("object"),
                   "tagger_date": (tag.get("tagger") or {}).get("date")}
c.check("tag v0.1.14 is the annotated tag a029066d pointing to commit 6f2c8f45",
        ref["object"]["type"] == "tag" and ref["object"]["sha"] == TAG_OBJECT
        and (tag.get("object") or {}).get("sha") == TAG_COMMIT, observed["tag"])

files: dict[str, Any] = {}
for label, gitref in (("v0.1.14", "v0.1.14"), ("#453 head", PR_HEAD), ("#453 merge", PR_MERGE),
                      ("v0.1.13", "v0.1.13"), ("main", "main")):
    meta = api(f"/contents/{L.DOC_PATH}?ref={gitref}")
    raw = L.fetch(f"{RAW}/{gitref}/{L.DOC_PATH}")
    files[label] = {"ref": gitref, "blob": meta["sha"], "size": meta["size"], "sha256": hashlib.sha256(raw).hexdigest(),
                    "api_content_equals_raw": base64.b64decode(meta["content"]) == raw, "text": raw.decode("utf-8")}
observed["guidance_file"] = {k: {x: v[x] for x in ("ref", "blob", "size", "sha256", "api_content_equals_raw")}
                             for k, v in files.items()}
c.check("docs/runtime-database-upgrades.md at tag v0.1.14 is git blob 62a77d88, SHA-256 8e4af607..., the same file as at "
        "the #453 head 2d8fd446 and merge 65ad947b; v0.1.13 has a different file",
        files["v0.1.14"]["blob"] == files["#453 head"]["blob"] == files["#453 merge"]["blob"]
        == "62a77d88e52b09ff677a99c91d72e10559b88ea5" and files["v0.1.14"]["sha256"] == L.DOC_SHA256
        and files["v0.1.13"]["blob"] != files["v0.1.14"]["blob"]
        and all(v["api_content_equals_raw"] for v in files.values()),
        {k: v["blob"][:8] for k, v in files.items()})

tag_text, old_text = flat(files["v0.1.14"]["text"]), flat(files["v0.1.13"]["text"])
MARKERS = {
    "section": "## SQLite runtime home backup",
    "no single command": "No single command backs up the whole runtime home.",
    "refusal": "refuses any other id, which other modules set on their own databases, with exit code 1",
    "table row (brain working state)": "| `state/brain/sessions.db` (brain working state) | none | accepted |",
    "table row (brain)": "| `state/brain/brain.db` | brain | refused |",
    "backup step 1": "Back up each accepted file with `openminion storage backup --sqlite <file> --output <backup>`.",
    "backup step 2 (shell)": "sqlite3 <file> \".backup '<backup>'\"",
    "backup step 2 (python)": "src.backup(dst); dst.close(); src.close()\" <file> <backup>",
    "backup step 3": "Copy the other files under the data root as they are, except the `<file>-journal`, "
                     "`<file>-wal` and `<file>-shm` companions of the SQLite files.",
    "backup step 4": "Record a checksum of each backup, for example with `sha256sum`.",
    "rollback step 1": "Stop every process that uses the runtime home, readers included.",
    "rollback step 2": "Check every backup before you replace anything: it matches the checksum you recorded, and "
                       "`PRAGMA integrity_check` on it",
    "rollback step 3": "Keep each set together and do not delete any of it",
    "rollback step 4": "Then copy the other files back, without any `-journal`, `-wal` or `-shm`.",
    "rollback step 5": "Check that no `<file>-journal`, `<file>-wal` or `<file>-shm` is next to a restored file before "
                       "you start any process.",
    "working state location": "in the `working_state` table of the brain's session store",
    "typed save boundary": "the first runtime turn after the upgrade can write the reset's row without them, and the "
                           "brain's next typed save in that turn adds them",
    "rollback boundary": "Rolling back means restoring the matching pre-upgrade backup with the matching older package.",
}
marker_hits = {k: {"v0.1.14": flat(v) in tag_text, "v0.1.13": flat(v) in old_text} for k, v in MARKERS.items()}
observed["guidance_markers"] = marker_hits
c.check("the text at the tag carries the corrected guidance: the 'SQLite runtime home backup' section with its file "
        "table, backup steps 1-4 and rollback steps 1-5, the working-state location and the typed-save paragraph",
        all(v["v0.1.14"] for v in marker_hits.values()), {k: v["v0.1.14"] for k, v in marker_hits.items()})
c.check("control: the v0.1.13 text has none of it except the general rollback sentence, which both versions keep",
        all(not v["v0.1.13"] for k, v in marker_hits.items() if k != "rollback boundary")
        and marker_hits["rollback boundary"]["v0.1.13"],
        {k: v["v0.1.13"] for k, v in marker_hits.items()})
observed["main_equals_tag"] = files["main"]["blob"] == files["v0.1.14"]["blob"]

readme = L.fetch(f"{RAW}/v0.1.14/docs/README.md").decode("utf-8")
entry = re.search(r"- \[`runtime-database-upgrades\.md`\]\(runtime-database-upgrades\.md\):[^\n]*(\n  [^\n]*)*", readme)
observed["docs_readme_entry"] = entry.group(0) if entry else None
c.check("docs/README.md at the tag indexes the guide as covering 'backup and rollback of every database in a SQLite "
        "runtime home'",
        bool(entry) and "backup and rollback of every database in a SQLite runtime home" in flat(entry.group(0)),
        observed["docs_readme_entry"])

# ======================================================== CHANGELOG at the tag and on main
changelog: dict[str, Any] = {}
for gitref in ("v0.1.14", "main"):
    text = L.fetch(f"{RAW}/{gitref}/CHANGELOG.md").decode("utf-8")
    heading, item = None, None
    for block in re.split(r"\n(?=## )", text):
        if "Refs #452" in block:
            heading = block.splitlines()[0]
            item = next((b for b in re.split(r"\n(?=- )", block) if "Refs #452" in b), None)
            break
    changelog[gitref] = {"heading": heading, "entry": flat(item) if item else None}
observed["changelog"] = changelog
c.check("CHANGELOG.md at the tag has the #452 entry under '## Current package line - 2026-10-27' (it names "
        "docs/runtime-database-upgrades.md), and main's CHANGELOG, which PyPI's 'Changelog' link opens, has the same "
        "entry",
        changelog["v0.1.14"]["heading"] == "## Current package line - 2026-10-27"
        and "docs/runtime-database-upgrades.md" in (changelog["v0.1.14"]["entry"] or "")
        and changelog["main"]["entry"] == changelog["v0.1.14"]["entry"],
        {k: v["heading"] for k, v in changelog.items()})

# ======================================================== GitHub release and compare
rel = api("/releases/tags/v0.1.14")
compare = api("/compare/v0.1.13...v0.1.14")
compare_files = {f["filename"]: f["status"] for f in compare.get("files") or []}
observed["github_release"] = {"name": rel.get("name"), "tag_name": rel.get("tag_name"), "draft": rel.get("draft"),
                              "prerelease": rel.get("prerelease"), "published_at": rel.get("published_at"),
                              "html_url": rel.get("html_url"), "assets": [a["name"] for a in rel.get("assets") or []],
                              "body": rel.get("body")}
observed["compare_v0.1.13...v0.1.14"] = {"status": compare.get("status"), "ahead_by": compare.get("ahead_by"),
                                          "files_listed": len(compare_files),
                                          "pr_files": {f: compare_files.get(f) for f in PR_FILES}}
c.check("the GitHub release v0.1.14 (published, no assets) lists 'Clarify SQLite runtime-home backup and rollback "
        "coverage, including WAL/journal companions and typed brain-state boundaries.' and links the compare "
        "v0.1.13...v0.1.14, which includes the three #453 files",
        rel.get("tag_name") == "v0.1.14" and rel.get("draft") is False and rel.get("prerelease") is False
        and "Clarify SQLite runtime-home backup and rollback coverage, including WAL/journal companions and typed "
            "brain-state boundaries." in (rel.get("body") or "")
        and "compare/v0.1.13...v0.1.14" in (rel.get("body") or "")
        and all(compare_files.get(f) == "modified" for f in PR_FILES),
        {"published_at": rel.get("published_at"), "pr_files": observed["compare_v0.1.13...v0.1.14"]["pr_files"]})

# ======================================================== PyPI and the distributions
pypi = json.loads(L.fetch("https://pypi.org/pypi/openminion/0.1.14/json"))
desc = pypi["info"].get("description") or ""
urls = pypi["info"].get("project_urls") or {}
doc_site = status_of(urls.get("Documentation", "https://www.openminion.com/docs"))
doc_page = status_of(urls.get("Documentation", "https://www.openminion.com/docs").rstrip("/")
                     + "/runtime-database-upgrades")
observed["pypi"] = {"version": pypi["info"]["version"], "description_bytes": len(desc),
                    "description_mentions": {k: desc.count(k) for k in ("runtime-database-upgrades",
                                                                        "SQLite runtime home", "#452", "backup")},
                    "description_doc_links": sorted(set(re.findall(r"\]\((docs/[^)]*)\)", desc))),
                    "project_urls": urls,
                    "documentation_site": {"url": urls.get("Documentation"), "status": doc_site["status"],
                                           "final_url": doc_site["final_url"],
                                           "mentions_guide": b"runtime-database-upgrades" in doc_site["body"]
                                           or b"SQLite runtime home" in doc_site["body"]},
                    "documentation_page_guess": {"url": urls.get("Documentation", "").rstrip("/")
                                                 + "/runtime-database-upgrades", "status": doc_page["status"]}}
dists: dict[str, Any] = {}
for f in pypi["urls"]:
    body = L.fetch(f["url"])
    (WORK / "dl" / f["filename"]).write_bytes(body)
    if f["packagetype"] == "bdist_wheel":
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            names = zf.namelist()
            texts = [zf.read(n) for n in names if not n.endswith((".png", ".jpg", ".ico", ".woff2"))]
    else:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tf:
            members = [m for m in tf.getmembers() if m.isfile()]
            names = [m.name for m in members]
            texts = [tf.extractfile(m).read() for m in members]  # type: ignore[union-attr]
    dists[f["filename"]] = {
        "packagetype": f["packagetype"], "sha256_matches_pypi": hashlib.sha256(body).hexdigest() == f["digests"]["sha256"],
        "members": len(names), "docs_members": [n for n in names if "/docs/" in n or n.startswith("docs/")],
        "changelog_members": [n for n in names if n.endswith("CHANGELOG.md")],
        "members_naming_the_guide": sum(1 for t in texts if b"runtime-database-upgrades" in t),
        "members_with_the_section": sum(1 for t in texts if b"SQLite runtime home backup" in t)}
observed["pypi_distributions"] = dists
c.check("not in the package: the PyPI wheel and sdist (SHA-256 equal to PyPI's digests) contain no docs/ directory and "
        "no CHANGELOG.md, and none of their files names runtime-database-upgrades or holds the 'SQLite runtime home "
        "backup' section; the PyPI project description (the README) does not mention the guide",
        len(dists) == 2 and all(d["sha256_matches_pypi"] and not d["docs_members"] and not d["changelog_members"]
                                and d["members_naming_the_guide"] == 0 and d["members_with_the_section"] == 0
                                for d in dists.values())
        and observed["pypi"]["description_mentions"]["runtime-database-upgrades"] == 0
        and observed["pypi"]["description_mentions"]["SQLite runtime home"] == 0,
        {k: {x: d[x] for x in ("packagetype", "members", "docs_members", "members_naming_the_guide")}
         for k, d in dists.items()})

dist = metadata.distribution("openminion")
record = [str(p) for p in (dist.files or [])]
pkg_dir = Path(dist.locate_file("openminion"))
naming = [str(p.relative_to(pkg_dir.parent)).replace("\\", "/") for p in pkg_dir.rglob("*")
          if p.is_file() and p.suffix in (".py", ".md", ".txt", ".json", ".yaml", ".toml")
          and b"runtime-database-upgrades" in p.read_bytes()]
observed["installed"] = {"dist_version": dist.version, "record_entries": len(record),
                         "record_docs_entries": [r for r in record if "runtime-database-upgrades" in r
                                                 or r.replace("\\", "/").startswith("docs/")],
                         "installed_files_naming_the_guide": naming}
c.check("the installed package (the same wheel) has no guidance file in its RECORD, and no installed file names it",
        dist.version == "0.1.14" and observed["installed"]["record_docs_entries"] == [] and naming == [],
        {"record_entries": len(record), "naming": naming})
observed["where_published"] = {
    "guidance_text": f"GitHub, tag v0.1.14: {L.DOC_PATH} (indexed from docs/README.md)",
    "changelog": "CHANGELOG.md at the tag and on main (PyPI 'Changelog' link)",
    "release_notes": "GitHub release v0.1.14: a one-line summary and the compare link, not the guidance text",
    "not_published_in": "the wheel, the sdist, the PyPI description; the Documentation site's guessed page: "
                        f"HTTP {doc_page['status']}"}

s = c.summary()
write_result(OUT, {"info": info, "observed": observed, "checks": c.items, "summary": s})
print(f"SUMMARY passed={s['passed']} failed={s['failed']} total={s['total']}")
sys.exit(0 if c.all_pass else 1)
