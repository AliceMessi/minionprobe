"""Issue #373: after one echo SDK turn and runtime.close(), with automatic GC disabled,
try to remove the whole runtime root and list what blocks it (native Windows).
Run from an openminion checkout: PYTHONPATH="src;." python rmtree_373.py
"""
import gc, json, os, shutil
from pathlib import Path
from tempfile import mkdtemp
gc.disable()
from openminion import Agent
from tests.api.test_artifact_runtime_roots import _echo_runtime
root = Path(mkdtemp()) / "root"
runtime = _echo_runtime(root)
with Agent(runtime=runtime) as agent:
    agent.run("hello")
runtime.close()
blocked = []
def onerror(func, path, exc):
    blocked.append(os.path.relpath(path, root).replace(os.sep, "/"))
shutil.rmtree(root, onerror=onerror)  # Python 3.11
print(json.dumps({"blocked": sorted(set(blocked)), "root_removed": not root.exists()}))
