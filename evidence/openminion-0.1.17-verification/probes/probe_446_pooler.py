"""#446 item 5: the documented pooler limitation, with PgBouncer in transaction pooling.

    python -I -X utf8 probe_446_pooler.py WORK OUT.json

Adapted from evidence/openminion-446/probes/pgbouncer_probe.py (the PR's review probe) to the installed
package and the real `openminion storage migrate` console script. PgBouncer (the distribution's
package) runs in front of the runner's PostgreSQL in transaction pooling, with its defaults, with
`ignore_startup_parameters = options` and with `track_extra_parameters = search_path`. For each, on a
new database: what a connection with `options=-c search_path=...` gets; the memory, registry and A2A
audit stores through PgBouncer (refused, nothing written); runtime storage through PgBouncer (its URL
is unchanged); `storage migrate --backend postgres` through PgBouncer (every module, then a rerun);
then the stores pointed at the server directly. This checks the published limitation; it does not
certify any pooler.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sqlalchemy as sa  # noqa: E402

from _common import Checks, banner, quiet, write_result  # noqa: E402
import probe_446_lib as L  # noqa: E402

PORT = 6433


def start(work: Path, extra: str) -> subprocess.Popen:
    server = sa.engine.make_url(L.BASE)
    (work / "users.txt").write_text(f'"{server.username}" "{server.password}"\n', encoding="utf-8")
    ini = work / "pgbouncer.ini"
    ini.write_text(
        f"[databases]\n* = host={server.host} port={server.port or 5432}\n\n[pgbouncer]\n"
        f"listen_addr = 127.0.0.1\nlisten_port = {PORT}\nauth_type = plain\n"
        f"auth_file = {work / 'users.txt'}\npool_mode = transaction\nunix_socket_dir =\n{extra}\n",
        encoding="utf-8")
    proc = subprocess.Popen(["pgbouncer", str(ini)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(1.5)
    return proc


def stop(proc: subprocess.Popen) -> str:
    proc.terminate()
    out, _ = proc.communicate(timeout=10)
    return "\n".join([line for line in out.splitlines() if "options" in line or "WARNING" in line][-4:])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    ap.add_argument("out")
    a = ap.parse_args()
    quiet()
    version = subprocess.run(["pgbouncer", "--version"], capture_output=True, text=True).stdout.splitlines()[:1]
    info = banner("probe_446_pooler", server=L.server_version(), pgbouncer=version)
    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    c = Checks()
    results = {}
    for label, extra in (("defaults", ""), ("ignore_startup_parameters = options", "ignore_startup_parameters = options"),
                         ("track_extra_parameters = search_path", "track_extra_parameters = search_path")):
        root = work / label.split()[0]
        root.mkdir(parents=True, exist_ok=True)
        direct = L.new_db(f"pool_{label.split()[0]}")
        pooled = sa.engine.make_url(direct).set(host="127.0.0.1", port=PORT).render_as_string(hide_password=False)
        proc = start(root, extra)
        r: dict = {}
        try:
            eng = sa.create_engine(L.with_options(pooled, '-c search_path="probe_schema"'))
            r["plain"] = L.attempt(lambda: eng.connect().execute(sa.text("SELECT current_setting('search_path')")).scalar())
            eng.dispose()
            before = L.snapshot(direct)
            r["stores"] = {st: L.attempt(lambda st=st: L.open_close(st, pooled, root / st))
                           for st in ("memory_store", "registry_store", "a2a_audit_store")}
            r["unchanged_after_stores"] = L.snapshot(direct) == before
            r["runtime"] = L.attempt(lambda: L.start_runtime(pooled, root / "rt"))
            run = L.migrate(pooled, root)
            r["migrate"] = {"exit": run["exit"], "statuses": {m: (run["entries"].get(m) or {}).get("status")
                                                              for m in L.WORKING_MODULES}}
            rerun = L.migrate(pooled, root)
            r["rerun"] = rerun["exit"]
            r["schemas"] = L.schemas(direct)
            r["direct_stores"] = {st: L.attempt(lambda st=st: L.open_close(st, direct, root / f"direct-{st}"))[0]
                                  for st in L.STORES}
            r["module_ledgers"] = {m: (L.snapshot(direct).get(f"public__{m}", {}).get("ledgers", {})
                                       .get("alembic_version")) == [L.module_head(m)] for m in L.WORKING_MODULES}
        finally:
            r["pgbouncer_log"] = stop(proc)
        results[label] = r
        refusals = {st: msg for st, (ok, msg) in r["stores"].items() if not ok}
        if label == "defaults":
            c.check("pooler (PgBouncer defaults, transaction pooling): a connection with options=-c search_path is "
                    "rejected (\"unsupported startup parameter in options: search_path\"), so the memory, registry "
                    "and A2A audit stores cannot connect; the database is unchanged",
                    not r["plain"][0] and "unsupported startup parameter in options: search_path" in r["plain"][1]
                    and len(refusals) == 3 and all("unsupported startup parameter" in m for m in refusals.values())
                    and r["unchanged_after_stores"], {"plain": L.short(r["plain"][1], 200),
                                                       "stores": {k: L.short(v, 200) for k, v in refusals.items()}})
        else:
            c.check(f"pooler ({label}): the connection is accepted but keeps search_path '\"$user\", public'; the "
                    "memory, registry and A2A audit stores are refused before any write with the landing message "
                    "(\"did not apply the URL's options parameter\", \"Connect module stores directly\", \"Nothing "
                    "was written\"); the database is unchanged",
                    r["plain"] == (True, '"$user", public') and len(refusals) == 3
                    and all("did not apply the URL's options parameter" in m and "Connect module stores directly" in m
                            and L.NOTHING in m for m in refusals.values()) and r["unchanged_after_stores"],
                    {"plain": r["plain"], "stores": {k: L.short(v, 300) for k, v in refusals.items()}})
        c.check(f"pooler ({label}): runtime storage (configured URL unchanged) starts through PgBouncer at v15",
                r["runtime"] == (True, "15"), r["runtime"])
        c.check(f"pooler ({label}): `openminion storage migrate --backend postgres` through PgBouncer exits 0 with "
                "every module ok in public__<id> at its head, and a rerun exits 0",
                r["migrate"]["exit"] == 0 and all(v == "ok" for v in r["migrate"]["statuses"].values())
                and all(r["module_ledgers"].values()) and r["rerun"] == 0, r["migrate"])
        c.check(f"pooler ({label}): pointed at the server directly, every module store opens",
                all(r["direct_stores"].values()), r["direct_stores"])
        L.drop_all()
    write_result(Path(a.out), {"banner": info, "summary": c.summary(), "checks": c.items,
                               "results": results, "commands": L.RUNS})
    s = c.summary()
    print(f"SUMMARY passed={s['passed']} failed={s['failed']} total={s['total']}")
    return 0 if c.all_pass else 1


if __name__ == "__main__":
    os.environ.pop("PGOPTIONS", None)
    sys.exit(main())
