"""#446: the installed package's #536 files, and their identity with the evidence that is reused.

    python -I -X utf8 probe_446_package.py OUT.json [--expect v0.1.17|v0.1.16|v0.1.18]

For every packaged file PR #536 changed (probe_446_expected.BLOBS, generated from a clone of
openminion/openminion), the git blob hash of the installed file is computed and compared with the
blob at the expected tag. The same table gives the blob at the PR's reviewed head bfa677d8 and at
the round-2 head 370be163, whose runs are in evidence/openminion-446: equal blobs are the stated
identity link between that evidence and the published artifact. The installed
postgres_histories.json is also compared with evidence/openminion-446/data/postgres_histories.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import openminion  # noqa: E402

from _common import Checks, banner, write_result  # noqa: E402
import probe_446_expected as X  # noqa: E402


def git_blob(path: Path) -> str | None:
    if not path.exists():
        return None
    raw = path.read_bytes()
    return hashlib.sha1(b"blob %d" % len(raw) + bytes([0]) + raw).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--expect", default="v0.1.17")
    a = ap.parse_args()
    info = banner("probe_446_package", expect=a.expect, commits=X.COMMITS)
    site = Path(openminion.__file__).resolve().parent.parent
    c = Checks()
    rows = {}
    for rel, blobs in X.BLOBS.items():
        rows[rel] = {"installed": git_blob(site / rel), **blobs}
    match = {rel: r["installed"] == r[a.expect] for rel, r in rows.items()}
    c.check(f"every packaged file PR #536 changed ({len(rows)}) is installed exactly as at {a.expect} "
            "(git blob of the installed file)", all(match.values()), {k: v for k, v in match.items() if not v})
    same = {rel: r["v0.1.17"] == r["bfa677d8"] == r["370be163"] for rel, r in rows.items()}
    c.check("identity link: each of those files has the same blob at v0.1.17, at the reviewed head bfa677d8 and at the "
            "round-2 head 370be163", all(same.values()), {k: v for k, v in same.items() if not v})
    hist = site / "openminion/modules/storage/migrations/postgres_histories.json"
    if hist.exists():
        raw = hist.read_bytes()
        data = json.loads(raw.decode("utf-8"))
        counts = {"histories": len(data["histories"]), "signatures": len(data["signatures"]),
                  "engine_tables": sorted(data["engine_tables"]), "runtime_versions": len(data["runtime_versions"]),
                  "current": sum(1 for h in data["histories"] if "current" in h["measured"]),
                  "alembic_base": sum(1 for h in data["histories"] if h["ledger"] == "alembic base")}
        c.check("the installed postgres_histories.json is byte-identical to evidence/openminion-446/data/"
                "postgres_histories.json (the round-2 head's file)",
                hashlib.sha256(raw).hexdigest() == X.ROUND2_HISTORIES_SHA256,
                {"sha256": hashlib.sha256(raw).hexdigest(), **counts})
        c.check("it lists 98 histories over 127 signatures, 11 of them `alembic base`, and the storage engine's "
                "three tables", counts["histories"] == 98 and counts["signatures"] == 127 and counts["alembic_base"] == 11
                and counts["engine_tables"] == ["core_events", "core_sidecar_rows", "sidecar_ingest_log"], counts)
    else:
        c.check("postgres_histories.json is installed", False, str(hist))
    write_result(Path(a.out), {"banner": info, "summary": c.summary(), "checks": c.items, "files": rows})
    s = c.summary()
    print(f"SUMMARY passed={s['passed']} failed={s['failed']} total={s['total']}")
    return 0 if c.all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
