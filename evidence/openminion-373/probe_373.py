import gc, os, sqlite3, traceback
from pathlib import Path
from tempfile import mkdtemp

gc.disable()  # make the result independent of when a full collection happens to run
opened = {}  # id(conn) -> opening site (no strong reference kept)
_connect = sqlite3.connect
def _tracking_connect(*a, **k):
    conn = _connect(*a, **k)
    site = [f for f in traceback.extract_stack()[:-1] if "openminion" in f.filename][-3:]
    opened[id(conn)] = " <- ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(site))
    return conn
sqlite3.connect = sqlite3.dbapi2.connect = _tracking_connect

from openminion import Agent
from tests.api.test_artifact_runtime_roots import _echo_runtime

root = Path(mkdtemp()) / "root"
store = root / ".openminion" / "authored_tools" / "store.sqlite"
runtime = _echo_runtime(root)
with Agent(runtime=runtime) as agent:
    print("run_state:", agent.run("hello").run_state)
runtime.close(); runtime.close()

def check(label):
    try:
        runtime.authored_tools._store._conn.execute("SELECT 1"); own = "open"
    except sqlite3.ProgrammingError as exc:
        own = f"ProgrammingError: {exc}"
    try:
        os.replace(store, f"{store}.x"); os.replace(f"{store}.x", store); rename = "ok"
    except PermissionError as exc:
        rename = f"PermissionError [WinError {exc.winerror}]"
    still_open = []
    for c in gc.get_objects():
        if type(c) is sqlite3.Connection:
            try:
                if str(store) in [r[2] for r in c.execute("PRAGMA database_list")]:
                    still_open.append(opened.get(id(c)))
            except sqlite3.ProgrammingError:
                pass
    print(f"{label}: store's own conn: {own} | rename store.sqlite: {rename} | open connections to it: {len(still_open)}")
    for site in still_open:
        print("   ", site)

check("after runtime.close() x2")
print("gc.collect():", gc.collect())
check("after gc.collect()")
