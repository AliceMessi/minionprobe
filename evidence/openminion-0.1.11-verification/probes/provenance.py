"""Package provenance for the installed openminion (default 0.1.11).

    python -I provenance.py <download_dir> <out.json> [version]

1. Read PyPI's JSON metadata for the version (wheel filename, URL, sha256).
2. Download that wheel into a separate directory (never executed or imported)
   and check its sha256 against PyPI's digest.
3. Compare the wheel's RECORD with the installed dist-info RECORD, and hash
   every installed file listed there against the RECORD's sha256.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import platform
import sqlite3
import sys
import urllib.request
import zipfile
from importlib import metadata
from pathlib import Path

DL = Path(sys.argv[1])
OUT = Path(sys.argv[2])
VERSION = sys.argv[3] if len(sys.argv) > 3 else "0.1.11"
DL.mkdir(parents=True, exist_ok=True)

import openminion  # noqa: E402

with urllib.request.urlopen(f"https://pypi.org/pypi/openminion/{VERSION}/json", timeout=60) as r:  # noqa: S310
    meta = json.load(r)
files = meta["urls"]
wheels = [f for f in files if f["packagetype"] == "bdist_wheel"]
sdists = [f for f in files if f["packagetype"] == "sdist"]
wheel = wheels[0]
target = DL / wheel["filename"]
with urllib.request.urlopen(wheel["url"], timeout=120) as r:  # noqa: S310
    target.write_bytes(r.read())
wheel_sha = hashlib.sha256(target.read_bytes()).hexdigest()

dist = metadata.distribution("openminion")
site = Path(dist.locate_file(""))
installed_record = (Path(dist._path) / "RECORD").read_text(encoding="utf-8")  # type: ignore[attr-defined]
with zipfile.ZipFile(target) as zf:
    record_name = next(n for n in zf.namelist() if n.endswith(".dist-info/RECORD"))
    wheel_record = zf.read(record_name).decode("utf-8")


def rows(text: str) -> dict[str, str]:
    out = {}
    for row in csv.reader(io.StringIO(text)):
        if row and row[1]:
            out[row[0]] = row[1]
    return out


wheel_rows = rows(wheel_record)
installed_rows = rows(installed_record)
checked = mismatched = missing = 0
for rel, digest in wheel_rows.items():
    algo, _, b64 = digest.partition("=")
    path = site / rel
    if not path.exists():
        missing += 1
        continue
    actual = base64.urlsafe_b64encode(hashlib.new(algo, path.read_bytes()).digest()).rstrip(b"=").decode()
    checked += 1
    if actual != b64:
        mismatched += 1

extras = {}
for name in ("sqlalchemy", "psycopg", "psycopg-binary", "alembic"):
    try:
        extras[name] = metadata.version(name)
    except metadata.PackageNotFoundError:
        extras[name] = None

result = {
    "openminion.__file__": openminion.__file__,
    "openminion.__version__": getattr(openminion, "__version__", None),
    "dist_version": dist.version,
    "installer": (dist.read_text("INSTALLER") or "").strip(),
    "python": sys.version.split()[0],
    "platform": platform.platform(),
    "sqlite3.sqlite_version": sqlite3.sqlite_version,
    "related_packages": extras,
    "pypi_release_files": [
        {"filename": f["filename"], "packagetype": f["packagetype"], "sha256": f["digests"]["sha256"],
         "upload_time_iso_8601": f.get("upload_time_iso_8601"), "size": f.get("size")}
        for f in files
    ],
    "wheel_filename": wheel["filename"],
    "wheel_sha256_pypi": wheel["digests"]["sha256"],
    "wheel_sha256_downloaded": wheel_sha,
    "wheel_sha256_matches_pypi": wheel_sha == wheel["digests"]["sha256"],
    "wheel_RECORD_rows_all_in_installed_RECORD": set(wheel_rows.items()) <= set(installed_rows.items()),
    "installed_RECORD_extra_rows": sorted(set(installed_rows) - set(wheel_rows)),
    "wheel_RECORD_entries_with_hash": len(wheel_rows),
    "installed_files_hash_checked": checked,
    "installed_files_hash_mismatched": mismatched,
    "installed_files_missing": missing,
    "sdist_count": len(sdists),
}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result, indent=2))
