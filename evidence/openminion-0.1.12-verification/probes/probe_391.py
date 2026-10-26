"""#391 on the installed package: memctl with a `store.backend: remote` YAML config.

    python -I -X utf8 probe_391.py <work_dir> <out.json>

A local HTTP server on 127.0.0.1 plays the remote memory store (the
`memory_transport.v1` envelope that RemoteMemoryTransport POSTs). It records
every request and answers by URL path:
  /ok    -> {"ok": true, "data": {"records": [<one record>]}}
  /fail  -> {"ok": false, "error": {"code": "BACKEND_UNAVAILABLE"}} (retryable)
  /slow  -> sleeps 3 s, then answers like /ok
Nothing else is contacted.

Checks:
1. A real memctl YAML (`store.backend: remote`, `store.remote.{endpoint,
   timeout_seconds, max_retries}`) parsed by `load_config`: the StoreConfig
   carries the values.
2. `resolve_memory_backend` with the whole parsed MemctlConfig and with its
   `store` section: a RemoteMemoryStore whose transport has the configured
   endpoint, timeout and retries, including `max_retries: 0`.
3. Transport behaviour with those values: against /fail, `max_retries: 2` sends
   3 requests and `max_retries: 0` sends exactly 1; against /slow, a 1 s
   timeout with 0 retries fails with TIMEOUT after about 1 s and 1 request.
4. `memctl list` through the installed `memctl` console script (separate
   process, MEMCTL_CONFIG=<yaml>) and in process (typer CliRunner): exit 0,
   the served record printed, a `list` request received, no missing-endpoint
   error; the same command with the zero-retry config against /fail sends 1
   request, and with 2 retries sends 3.
5. Dictionary configs keep their behaviour: a nested `remote` mapping is used
   as a whole (top level before `store.remote`), with the dictionary defaults
   (5.0 s, 1 retry) when values are missing or falsey.
6. Missing endpoint is still rejected: by `load_config`, for a dictionary, for a
   typed StoreConfig built without an endpoint, and by `memctl list`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, outcome, quiet, write_result  # noqa: E402

import yaml  # noqa: E402

from openminion.modules.memory.config import load_config  # noqa: E402
from openminion.modules.memory.storage.factory import resolve_memory_backend  # noqa: E402

WORK = Path(sys.argv[1])
OUT = Path(sys.argv[2])
REQUESTS: list[dict] = []
RECORD = {
    "id": "remote-1", "scope": "agent:alpha", "type": "fact", "key": "pref:editor",
    "title": "Editor", "content": "Prefers vim.", "created_at": "2026-10-01T00:00:00Z",
    "updated_at": "2026-10-01T00:00:00Z",
}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        envelope = json.loads(body or b"{}")
        REQUESTS.append({"path": self.path, "operation": envelope.get("operation"),
                         "scopes": (envelope.get("payload") or {}).get("scopes")})
        if self.path.endswith("/fail"):
            reply = {"ok": False, "error": {"code": "BACKEND_UNAVAILABLE", "message": "probe failure"}}
        else:
            if self.path.endswith("/slow"):
                time.sleep(3)
            reply = {"ok": True, "data": {"records": [RECORD]}}
        data = json.dumps(reply).encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass

    def log_message(self, *args):
        pass


def write_yaml(path: Path, remote: dict | None) -> Path:
    store: dict = {"backend": "remote"}
    if remote is not None:
        store["remote"] = remote
    memctl = {"store": store, "defaults": {"confidence": {"user_said": 0.7}},
              "promotion": {}, "retrieval": {}, "retention": {}}
    path.write_text(yaml.safe_dump({"version": 1, "memctl": memctl}), encoding="utf-8")
    return path


def transport_of(resolved) -> dict:
    t = resolved.store._transport  # noqa: SLF001
    return {"store": type(resolved.store).__name__, "endpoint": t._endpoint,  # noqa: SLF001
            "timeout_seconds": t._timeout_seconds, "max_retries": t._max_retries}  # noqa: SLF001


def requests_since(mark: int) -> list[dict]:
    return REQUESTS[mark:]


def main() -> int:
    quiet()
    clear_override_env()
    info = banner("probe_391")
    checks = Checks()
    WORK.mkdir(parents=True, exist_ok=True)
    root = WORK / "run"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir()
    home = root / "home"
    home.mkdir()
    env = {"HOME": str(home), "USERPROFILE": str(home)}

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/memory"
    ok_ep, fail_ep, slow_ep = base + "/ok", base + "/fail", base + "/slow"

    def shown(value):
        text = json.dumps(value, default=str) if not isinstance(value, str) else value
        return text.replace(f"127.0.0.1:{server.server_address[1]}", "127.0.0.1:<port>")

    results: dict = {}

    # 1. Parse real YAML configs.
    cfg_ok = write_yaml(root / "remote.yaml", {"endpoint": ok_ep, "timeout_seconds": 2.5, "max_retries": 2})
    cfg_zero = write_yaml(root / "remote-zero.yaml", {"endpoint": ok_ep, "timeout_seconds": 2.5, "max_retries": 0})
    cfg_fail2 = write_yaml(root / "remote-fail-2.yaml", {"endpoint": fail_ep, "timeout_seconds": 2.5, "max_retries": 2})
    cfg_fail0 = write_yaml(root / "remote-fail-0.yaml", {"endpoint": fail_ep, "timeout_seconds": 2.5, "max_retries": 0})
    cfg_slow0 = write_yaml(root / "remote-slow-0.yaml", {"endpoint": slow_ep, "timeout_seconds": 1.0, "max_retries": 0})
    cfg_missing = write_yaml(root / "remote-missing.yaml", {"timeout_seconds": 2.5, "max_retries": 0})
    parsed = load_config(cfg_ok, env=env)
    zero = load_config(cfg_zero, env=env)
    store_fields = {k: getattr(parsed.store, k) for k in ("backend", "remote_endpoint", "remote_timeout_seconds", "remote_max_retries")}
    results["load_config(remote.yaml).store"] = shown(store_fields)
    checks.check("load_config fills StoreConfig remote fields from the YAML",
                 store_fields == {"backend": "remote", "remote_endpoint": ok_ep, "remote_timeout_seconds": 2.5, "remote_max_retries": 2},
                 shown(store_fields))
    checks.check("load_config keeps max_retries: 0", zero.store.remote_max_retries == 0, zero.store.remote_max_retries)

    # 2. resolve_memory_backend with the parsed config, whole and store section.
    db_path = root / "unused.db"
    expected = {"store": "RemoteMemoryStore", "endpoint": ok_ep, "timeout_seconds": 2.5}
    for label, cfg, retries in (
        ("whole MemctlConfig", parsed, 2), ("MemctlConfig.store", parsed.store, 2),
        ("whole MemctlConfig, max_retries 0", zero, 0), ("MemctlConfig.store, max_retries 0", zero.store, 0),
    ):
        try:
            got = transport_of(resolve_memory_backend(config=cfg, db_path=db_path))
        except Exception as exc:  # noqa: BLE001
            got = f"{type(exc).__name__}: {exc}"
        results[f"resolve_memory_backend({label})"] = shown(got)
        checks.check(f"resolve_memory_backend({label}) -> RemoteMemoryStore with the configured transport",
                     got == {**expected, "max_retries": retries}, shown(got))
    checks.check("no SQLite file was created by resolving the remote backend", not db_path.exists(), db_path.exists())

    # 3. Transport behaviour with the configured values.
    from openminion.modules.memory.storage.base import ListQueryOptions

    def list_through(cfg_path: Path):
        try:
            store = resolve_memory_backend(config=load_config(cfg_path, env=env).store, db_path=db_path).store
        except Exception as exc:  # noqa: BLE001 - the 0.1.11 control fails here
            return f"{type(exc).__name__}: {exc}", 0, 0.0
        mark = len(REQUESTS)
        start = time.monotonic()
        try:
            store.list(ListQueryOptions(scopes=["agent:alpha"], limit=5))
            result = "ok"
        except Exception as exc:  # noqa: BLE001
            result = f"{type(exc).__name__}: {exc}"
        return result, len(requests_since(mark)), round(time.monotonic() - start, 2)

    for label, cfg_path, want_requests in (("max_retries 2 against /fail", cfg_fail2, 3), ("max_retries 0 against /fail", cfg_fail0, 1)):
        result, count, _elapsed = list_through(cfg_path)
        results[f"store.list, {label}"] = {"result": shown(result), "requests": count}
        checks.check(f"store.list {label}: {want_requests} request(s), then BACKEND_UNAVAILABLE",
                     count == want_requests and "BACKEND_UNAVAILABLE" in result, {"requests": count, "result": shown(result)})
    result, count, elapsed = list_through(cfg_slow0)
    results["store.list, timeout 1.0 s, max_retries 0, against /slow (3 s)"] = {"result": shown(result), "requests": count, "elapsed_s": elapsed}
    checks.check("store.list with timeout_seconds 1.0 and 0 retries: TIMEOUT after about 1 s, 1 request",
                 "TIMEOUT" in result and count == 1 and 0.9 <= elapsed < 2.5, {"result": shown(result), "requests": count, "elapsed_s": elapsed})

    # 4. memctl list, console script and in process.
    memctl = Path(sys.executable).with_name("memctl.exe" if os.name == "nt" else "memctl")
    base_env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENMINION_", "MEMCTL_"))}
    base_env.update(env, NO_PROXY="127.0.0.1,localhost", PYTHONUTF8="1")

    def run_cli(cfg_path: Path):
        mark = len(REQUESTS)
        proc = subprocess.run([str(memctl), "list", "--scope", "agent:alpha", "--json"],
                              capture_output=True, text=True, env={**base_env, "MEMCTL_CONFIG": str(cfg_path)},
                              cwd=str(root), timeout=300)
        errors = [line.strip() for line in proc.stderr.splitlines() if "Error" in line or "error" in line]
        try:
            ids = [r.get("id") for r in json.loads(proc.stdout)]
        except ValueError:
            ids = None
        return {"exit_code": proc.returncode, "printed_record_ids": ids, "stdout": proc.stdout.strip()[:600],
                "last_error_line": shown(errors[-1]) if errors else "", "requests": requests_since(mark)}

    row = run_cli(cfg_ok)
    results["memctl list --scope agent:alpha --json (console script, MEMCTL_CONFIG=remote.yaml)"] = shown(row)
    checks.check("memctl console script lists the remote store's record (exit 0, no missing-endpoint error)",
                 row["exit_code"] == 0 and row["printed_record_ids"] == ["remote-1"] and "endpoint" not in row["last_error_line"],
                 {"exit_code": row["exit_code"], "ids": row["printed_record_ids"], "last_error_line": row["last_error_line"]})
    checks.check("the remote store received exactly one `list` request for agent:alpha",
                 [(r["operation"], r["scopes"]) for r in row["requests"]] == [("list", ["agent:alpha"])], row["requests"])
    for label, cfg_path, want in (("max_retries 0", cfg_fail0, 1), ("max_retries 2", cfg_fail2, 3)):
        row = run_cli(cfg_path)
        results[f"memctl list (console script) against /fail, {label}"] = shown(row)
        checks.check(f"memctl console script against /fail with {label}: exit != 0 after {want} request(s)",
                     row["exit_code"] != 0 and len(row["requests"]) == want and "BACKEND_UNAVAILABLE" in row["last_error_line"],
                     {"exit_code": row["exit_code"], "requests": len(row["requests"]), "last_error_line": row["last_error_line"]})

    from typer.testing import CliRunner

    from openminion.modules.memory import cli as memory_cli

    mark = len(REQUESTS)
    res = CliRunner().invoke(memory_cli._build_app(), ["list", "--scope", "agent:alpha", "--json"],
                             env={**env, "MEMCTL_CONFIG": str(cfg_ok), "OPENMINION_HOME": None, "OPENMINION_DATA_ROOT": None})
    in_proc = {"exit_code": res.exit_code, "output": res.output.strip()[:600],
               "exception": None if res.exception is None or isinstance(res.exception, SystemExit) else f"{type(res.exception).__name__}: {res.exception}",
               "requests": requests_since(mark)}
    results["memctl list in process (typer CliRunner, MEMCTL_CONFIG=remote.yaml)"] = shown(in_proc)
    checks.check("memctl list in process lists the remote record with one request",
                 res.exit_code == 0 and '"remote-1"' in res.output and len(in_proc["requests"]) == 1, shown(in_proc))

    # 5. Dictionary configs.
    def dict_case(config):
        try:
            return transport_of(resolve_memory_backend(config=config, db_path=db_path))
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"

    top = {"endpoint": base + "/top", "timeout_seconds": 3.5, "max_retries": 4}
    nested = {"endpoint": base + "/nested", "timeout_seconds": 7.0, "max_retries": 6}
    cases = {
        "top-level remote mapping": ({"backend": "remote", "remote": top}, {"endpoint": top["endpoint"], "timeout_seconds": 3.5, "max_retries": 4}),
        "store.remote mapping": ({"store": {"backend": "remote", "remote": nested}}, {"endpoint": nested["endpoint"], "timeout_seconds": 7.0, "max_retries": 6}),
        "both: the top-level mapping wins as a whole": (
            {"backend": "remote", "remote": {"endpoint": base + "/top"}, "store": {"backend": "remote", "remote": nested}},
            {"endpoint": base + "/top", "timeout_seconds": 5.0, "max_retries": 1}),
        "endpoint only: dictionary defaults": ({"store": {"backend": "remote", "remote": {"endpoint": ok_ep}}},
                                               {"endpoint": ok_ep, "timeout_seconds": 5.0, "max_retries": 1}),
        "falsey timeout/retries in a dictionary: defaults, as before": (
            {"store": {"backend": "remote", "remote": {"endpoint": ok_ep, "timeout_seconds": 0, "max_retries": 0}}},
            {"endpoint": ok_ep, "timeout_seconds": 5.0, "max_retries": 1}),
    }
    for label, (config, want) in cases.items():
        got = dict_case(config)
        results[f"dict config: {label}"] = shown(got)
        checks.check(f"dict config, {label}", got == {"store": "RemoteMemoryStore", **want}, shown(got))

    # 6. Missing endpoint.
    from openminion.modules.memory.config.models import SQLiteRuntimeConfig, StoreConfig

    missing = {
        "load_config(remote-missing.yaml)": outcome(lambda: load_config(cfg_missing, env=env)),
        "resolve_memory_backend(dict without endpoint)": outcome(
            lambda: resolve_memory_backend(config={"store": {"backend": "remote", "remote": {"timeout_seconds": 1}}}, db_path=db_path)),
        "resolve_memory_backend(StoreConfig(backend='remote') without endpoint)": outcome(
            lambda: resolve_memory_backend(config=StoreConfig(backend="remote", sqlite_path=None, sqlite=SQLiteRuntimeConfig(
                wal_mode=True, busy_timeout_ms=5000, fts5_enabled=True)), db_path=db_path)),
    }
    results.update({k: shown(v) for k, v in missing.items()})
    checks.check("load_config rejects a remote config without an endpoint",
                 missing["load_config(remote-missing.yaml)"].startswith("ConfigError") and "endpoint" in missing["load_config(remote-missing.yaml)"],
                 missing["load_config(remote-missing.yaml)"])
    for key in list(missing)[1:]:
        checks.check(f"{key} raises the missing-endpoint error",
                     missing[key] == "InvalidArgumentError: remote backend requires remote.endpoint", missing[key])
    row = run_cli(cfg_missing)
    results["memctl list (console script, MEMCTL_CONFIG=remote-missing.yaml)"] = shown(row)
    checks.check("memctl console script with no endpoint fails with the endpoint error and sends nothing",
                 row["exit_code"] != 0 and "endpoint" in row["last_error_line"] and not row["requests"],
                 {"exit_code": row["exit_code"], "last_error_line": row["last_error_line"]})

    server.shutdown()
    summary = checks.summary()
    write_result(OUT, {"info": info, "results": results, "checks": checks.items, "summary": summary})
    print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
    return 0 if checks.all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
