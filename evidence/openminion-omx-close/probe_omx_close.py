"""OMX transfer: which sqlite connections stay open after export_omx/import_omx return (GC disabled)."""
import gc, os, sqlite3, sys, traceback
from pathlib import Path
from tempfile import mkdtemp

gc.disable()
opened = {}
_connect = sqlite3.connect
def _tracking_connect(*a, **k):
    conn = _connect(*a, **k)
    site = [f for f in traceback.extract_stack()[:-1] if "openminion" in f.filename][-3:]
    opened[id(conn)] = " <- ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(site)) or "(test code)"
    return conn
sqlite3.connect = sqlite3.dbapi2.connect = _tracking_connect

from openminion.modules.storage.migrations import MigrationRunner, export_omx, import_omx
from openminion.modules.storage.migrations.module_ids import get_module_application_id

APP = get_module_application_id("secret")
root = Path(mkdtemp())
src = root / "secret.db"; src.touch()
assert MigrationRunner(module_id="secret", db_path=src, module_application_id=APP).migrate(target="head").success
c = _connect(str(src)); c.execute("INSERT INTO secrets(key, namespace, value, created_at, updated_at) VALUES ('k','default','v',1.0,1.0)"); c.commit(); c.close()

def still_open(path):
    out = []
    for o in gc.get_objects():
        if type(o) is sqlite3.Connection:
            try:
                if str(path) in [r[2] for r in o.execute("PRAGMA database_list")]:
                    out.append(opened.get(id(o), "?"))
            except sqlite3.ProgrammingError:
                pass
    return out

def try_rename(path):
    try:
        os.replace(path, f"{path}.x"); os.replace(f"{path}.x", path); return "ok"
    except PermissionError as exc:
        return f"PermissionError [WinError {exc.winerror}]"

manifest = export_omx(db_path=src, module_id="secret", module_application_id=APP, export_dir=root / "omx")
print(f"after export_omx: rename source: {try_rename(src)} | open connections to source: {len(still_open(src))}")
for s in still_open(src): print("   ", s)
target = root / "imported.db"
report = import_omx(omx_dir=root / "omx", target_db_path=target)
print(f"import success: {report.success}")
print(f"after import_omx: rename target: {try_rename(target)} | open connections to target: {len(still_open(target))}")
for s in still_open(target): print("   ", s)
print("gc.collect():", gc.collect() > 0)
print(f"after gc.collect(): rename target: {try_rename(target)} | open: {len(still_open(target))}")
print(sys.version.split()[0], sys.platform)
