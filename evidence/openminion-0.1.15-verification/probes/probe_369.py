"""#369 on the installed package: session metadata filters through the full runtime
and CLI on PostgreSQL runtime storage (ACP `session/list`, Focus directory
sessions), now that APIRuntime starts there (#437).

    python -I -X utf8 probe_369.py <work_dir> <out.json> [--backend sqlite|postgres]

  acp     the installed `openminion acp` stdio server (ACP v1 JSON-RPC: initialize,
          session/new, session/list), started by the CLI with the config's runtime
          storage. Sessions with other `surface` values, and an ACP session with NaN,
          NUL and a lone surrogate in other members, are written by a second, real
          APIRuntime on the same database. Expected lists are computed from an
          independent read of the sessions table (json.loads, surface == "acp" as a
          string, newest first by (updated_at, id), store limit 100). Then two
          filters at once through that runtime's SessionStore. Needs the `acp` extra.
  focus   the Focus runtime (openminion.cli.interactive.runtime.OpenMinionRuntime) over a
          real APIRuntime, built as `openminion` builds it for a Focus launch (target
          "focus", working_dir, bind_immediately=False): create_new_session,
          list_directory_sessions (all, limit=2), find_candidate_session, another
          agent's and another directory's sessions.
  focus-cli  (POSIX only) the real bare `openminion --dir <project>` Focus launch in a
          pseudo-terminal, then `/exit`; its directory session must be in the runtime
          database and listed by the Focus runtime. Its config file is the same plus
          `runtime.demo_mode: true` (what `openminion config --provider echo` writes):
          without it the onboarding gate treats the echo agent as demo-only and opens
          the setup wizard instead of Focus.
PostgreSQL: one new database; each server process (ACP, Focus CLI) uses its own
application_name, counted while it runs and after it exits; no runtime SQLite
database may appear at storage.path.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, quiet, write_result  # noqa: E402
from probe_437_lib import (  # noqa: E402
    ConnectionWatch,
    PgDatabase,
    clean_env,
    cli_argv,
    describe_exc,
    install_masking,
    mask,
    openminion_exe,
    pg_base,
    pg_versions,
    sqlite_files,
    storage_path,
    strip_env,
    write_config,
)

NUL = chr(0)
LONE_HIGH = chr(0xD800)
NAN = float("nan")
CASES: dict[str, Any] = {}
NOT_RUN: list[dict[str, str]] = []


def scenario(checks: Checks, name: str):
    def wrap(fn):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            checks.check(f"{name}: completes without an exception", False, observed=mask(describe_exc(exc))[:500])
            traceback.print_exc()
        return fn

    return wrap


class Setup:
    """One runtime database (a new PostgreSQL database, or the SQLite file at storage.path)."""

    def __init__(self, work: Path, backend: str, name: str, agents: tuple[str, ...]) -> None:
        self.backend = backend
        self.db = PgDatabase(f"om369_{name}") if backend == "postgres" else None
        self.root = work / f"{name}-{backend}"
        self.agents = agents
        self.cfg_server = self._cfg("agents.json", "server")
        self.cfg_probe = self._cfg("agents-probe.json", "probe")
        # The bare Focus launch first asks the onboarding route, which treats an `echo` agent as
        # demo-only and opens the setup wizard unless `runtime.demo_mode` is true (what
        # `openminion config --provider echo` writes). Same storage and application_name as the server.
        self.cfg_focus_cli = self._cfg("agents-focus-cli.json", "server", {"demo_mode": True})

    def _cfg(self, filename: str, role: str, runtime_extra: dict[str, Any] | None = None) -> Path:
        storage = {}
        if self.db is not None:
            app = self.db.app if role == "server" else f"{self.db.app}_probe"
            storage = {"backend": "postgres", "postgres_url": self.db.url(scheme="postgresql+psycopg", app=app)}
        return write_config(self.root, agents=self.agents, storage=storage, filename=filename,
                            runtime_extra=runtime_extra)

    def runtime(self):
        """A real APIRuntime of this probe process (its own application_name on PostgreSQL)."""
        from openminion import APIRuntime

        return APIRuntime.from_config_path(str(self.cfg_probe), home_root=self.root, data_root=self.root / ".openminion")

    def rows(self, sql: str) -> list[dict[str, Any]]:
        if self.db is not None:
            return self.db.rows(sql)
        import sqlite3

        if not storage_path(self.root).exists():
            return []
        conn = sqlite3.connect(str(storage_path(self.root)))
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql)]
        finally:
            conn.close()

    def sessions(self) -> list[dict[str, Any]]:
        return self.rows("SELECT id, target, session_key, metadata_json, updated_at FROM sessions")

    def finish(self, checks: Checks, label: str) -> None:
        files = sqlite_files(self.root)
        CASES[f"{label} sqlite files"] = files
        if self.db is not None:
            checks.check(f"{label}: no runtime SQLite database created at storage.path",
                         not storage_path(self.root).exists() and ".openminion/state.db" not in files,
                         observed={"storage.path exists": storage_path(self.root).exists(), "sqlite files": sorted(files)})


def loads(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------- ACP
class AcpProcess:
    """`openminion ... acp` as a subprocess, spoken to over stdio (ACP v1 JSON-RPC lines)."""

    def __init__(self, setup: Setup, stderr_path: Path) -> None:
        self._stderr = open(stderr_path, "wb")
        self.proc = subprocess.Popen(
            [openminion_exe(), *cli_argv(setup.root, setup.cfg_server, ["acp"])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr, env=clean_env(), cwd=str(setup.root),
        )
        self._lines: "queue.Queue[bytes | None]" = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self._next = 0

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def call(self, method: str, params: dict[str, Any], timeout: float = 240) -> dict[str, Any]:
        self._next += 1
        ident = self._next
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}) + "\n").encode())
            self.proc.stdin.flush()
        except OSError as exc:
            raise RuntimeError(f"acp process not accepting input ({type(exc).__name__}); exit={self.proc.poll()}") from exc
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = self._lines.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise RuntimeError(f"no reply to {method} within {timeout}s") from exc
            if line is None:
                raise RuntimeError(f"acp process closed stdout before replying to {method}; exit={self.proc.wait(30)}")
            msg = json.loads(line)
            if msg.get("id") == ident and ("result" in msg or "error" in msg):
                return msg

    def close(self) -> int | None:
        try:
            if self.proc.stdin:
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass
            return self.proc.wait(timeout=120)
        finally:
            self._stderr.close()


def acp_section(checks: Checks, work: Path, backend: str, label: str) -> None:
    try:
        import acp  # noqa: F401  (agent-client-protocol, the `acp` extra)
    except ImportError:
        NOT_RUN.append({"item": f"{label} acp", "reason": "the `acp` extra (agent-client-protocol) is not installed here"})
        return
    import openminion

    setup = Setup(work, backend, "acp", ("openminion",))
    ws = {n: (setup.root / f"ws-{n}") for n in ("a", "b", "c")}
    for p in ws.values():
        p.mkdir(parents=True, exist_ok=True)
    wa, wb = str(ws["a"].resolve()), str(ws["b"].resolve())
    obs: dict[str, Any] = {"database": setup.db.name if setup.db else None,
                           "server application_name": setup.db.app if setup.db else None}
    CASES[f"{label} acp"] = obs
    proc = AcpProcess(setup, setup.root / "acp-stderr.log")
    watch = ConnectionWatch(setup.db).__enter__()
    rc: int | None = None
    try:
        try:
            init = proc.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        except Exception as exc:  # noqa: BLE001
            rc = proc.close()
            watch.__exit__(None, None, None)
            tail = [ln for ln in (setup.root / "acp-stderr.log").read_text(encoding="utf-8", errors="replace").splitlines()
                    if ln.strip()][-3:]
            obs["start"] = {"error": mask(describe_exc(exc)), "exit_code": rc, "stderr_tail": [mask(t) for t in tail]}
            checks.check(f"{label} acp: `openminion acp` starts and answers initialize", False, observed=obs["start"])
            return
        result = init.get("result", {})
        version = result.get("agentInfo", {}).get("version")
        list_cap = (result.get("agentCapabilities", {}).get("sessionCapabilities") or {}).get("list")
        checks.check(f"{label} acp: `openminion acp` (stdio JSON-RPC) answers initialize with the installed version and "
                     "the session/list capability", version == openminion.__version__ and list_cap == {},
                     observed={"version": version, "list": list_cap})
        s1 = proc.call("session/new", {"cwd": str(ws["a"]), "mcpServers": []})["result"]["sessionId"]
        s2 = proc.call("session/new", {"cwd": str(ws["b"]), "mcpServers": []})["result"]["sessionId"]
        extra = {
            "cli-in-a": {"surface": "cli", "workspace_root": wa},
            "list-in-a": {"surface": ["acp"], "workspace_root": wa},
            "true-in-a": {"surface": True, "workspace_root": wa},
            "upper-in-a": {"surface": "ACP", "workspace_root": wa},
            "none-in-a": {"workspace_root": wa},
            "odd-acp-in-a": {"surface": "acp", "workspace_root": wa, "score": NAN, "note": "a" + NUL + "b" + LONE_HIGH},
        }
        rt = setup.runtime()
        try:
            for sid, meta in extra.items():
                rt.sessions.resolve_session(agent_id="openminion", channel="console", target="local-client",
                                            session_id=sid, metadata=meta)
            obs["writer record store"] = type(rt.runtime_storage.record_store).__name__
        finally:
            rt.close()

        def ref(workspace: str | None, limit: int = 100) -> list[str]:
            hits = []
            for r in setup.sessions():
                m = json.loads(str(r["metadata_json"]))
                if isinstance(m.get("surface"), str) and m["surface"] == "acp":
                    hits.append((str(r["updated_at"]), str(r["id"]), str(m.get("workspace_root", ""))))
            hits.sort(reverse=True)
            return [sid for _, sid, w in hits[:limit] if workspace is None or w == workspace]

        def listed(cwd: Path | None) -> Any:
            msg = proc.call("session/list", {"cwd": str(cwd)} if cwd else {})
            if "error" in msg:
                return f"error {msg['error'].get('code')}: {str(msg['error'].get('message'))[:200]}"
            return [item["sessionId"] for item in msg["result"]["sessions"]]

        in_a = listed(ws["a"])
        every = listed(None)
        in_b = listed(ws["b"])
        obs.update({"session/list cwd=a": in_a, "session/list": every, "session/list cwd=b": in_b})
        checks.check(f"{label} acp: session/list cwd=a lists only the ACP sessions of workspace a, newest first "
                     "(including one with NaN, NUL and a lone surrogate in other members)",
                     in_a == ref(wa) and set(in_a) == {s1, "odd-acp-in-a"}, observed=in_a)
        checks.check(f"{label} acp: session/list without cwd lists every ACP session newest first; surfaces cli, "
                     "[\"acp\"], true, \"ACP\" and missing are excluded",
                     every == ref(None) and set(every) == {s1, s2, "odd-acp-in-a"}, observed=every)
        checks.check(f"{label} acp: session/list cwd=b lists only the workspace-b session", in_b == [s2], observed=in_b)
        for _ in range(105):
            proc.call("session/new", {"cwd": str(ws["c"]), "mcpServers": []})
        capped = listed(None)
        obs["session/list with 108 ACP sessions"] = capped if isinstance(capped, str) else len(capped)
        checks.check(f"{label} acp: with 108 ACP sessions, session/list returns the newest 100 (the store limit), in order",
                     isinstance(capped, list) and len(capped) == 100 and capped == ref(None, 100),
                     observed=capped if isinstance(capped, str) else len(capped))
    finally:
        if rc is None:
            rc = proc.close()
            watch.__exit__(None, None, None)
    obs["acp exit code"] = rc
    checks.check(f"{label} acp: the acp server exits 0 on stdin EOF", rc == 0, observed=rc)
    if setup.db is not None:
        after = setup.db.connections_settled()
        obs["server connections"] = {"max while running": watch.max, "after exit": after}
        checks.check(f"{label} acp: the acp server connected to PostgreSQL under its application_name while running and "
                     "left no connection after it exited", watch.max > 0 and after["count"] == 0,
                     observed=obs["server connections"])
        rows = setup.db.rows("SELECT count(*) AS n FROM sessions WHERE target = 'local-client'")
        obs["postgres sessions (target local-client)"] = rows

    # Two filters at once, through a real runtime's SessionStore.
    rt = setup.runtime()
    try:
        got = [r.id for r in rt.sessions.list_sessions(limit=1000, metadata_filter={"surface": "acp", "workspace_root": wa})]
        miss = [r.id for r in rt.sessions.list_sessions(limit=1000, metadata_filter={"surface": "acp", "workspace_root": wb + "x"})]
    finally:
        rt.close()
    exp = sorted(ref(wa, 1000))
    checks.check(f"{label} acp: through a real runtime, list_sessions with two filters (surface=acp, workspace_root=a) "
                 "selects exactly workspace a's ACP sessions; a non-matching second filter selects none",
                 sorted(got) == exp and miss == [], observed={"matched": len(got), "expected": len(exp), "nonmatching": miss})
    setup.finish(checks, f"{label} acp")


# ---------------------------------------------------------------- Focus
def focus_runtime(rt: Any, working_dir: Path, agent_id: str | None = None):
    """The Focus runtime with the keyword arguments `_launch_terminal_focus` passes."""
    from openminion.cli.interactive.runtime import OpenMinionRuntime

    return OpenMinionRuntime(rt, target="focus", agent_id=agent_id, working_dir=str(working_dir),
                             bind_immediately=False, session_id=None)


def focus_section(checks: Checks, setup: Setup, label: str) -> None:
    project_a, project_b = setup.root / "project-a", setup.root / "project-b"
    project_a.mkdir(parents=True, exist_ok=True)
    project_b.mkdir(parents=True, exist_ok=True)
    obs: dict[str, Any] = {"database": setup.db.name if setup.db else None}
    CASES[f"{label} focus"] = obs
    try:
        rt = setup.runtime()
    except Exception as exc:  # noqa: BLE001
        obs["start"] = mask(describe_exc(exc))
        checks.check(f"{label} focus: APIRuntime.from_config_path starts", False, observed=obs["start"])
        return
    try:
        obs["record store"] = type(rt.runtime_storage.record_store).__name__
        fa = focus_runtime(rt, project_a)
        created = [fa.create_new_session() for _ in range(3)]
        other_dir = focus_runtime(rt, project_b).create_new_session()
        beta_id = focus_runtime(rt, project_a, agent_id="beta").create_new_session()
        rt.sessions.resolve_session(agent_id="alpha", channel="cli", target="focus", session_id="focus-odd",
                                    metadata={"working_dir": str(project_a.resolve()), "focus_mode": True,
                                              "score": NAN, "note": NUL})
        stored = rt.sessions.get_session(created[0])
        checks.check(f"{label} focus ({obs['record store']}): a new Focus session stores working_dir (resolved) and "
                     "focus_mode", stored is not None and stored.metadata.get("working_dir") == str(project_a.resolve())
                     and stored.metadata.get("focus_mode") is True,
                     observed=None if stored is None else {k: stored.metadata.get(k) for k in ("working_dir", "focus_mode")})
        rows = sorted(rt.sessions.list_sessions(limit=1000, target="focus"), key=lambda r: (str(r.updated_at), str(r.id)),
                      reverse=True)
        expected = [r.id for r in rows if r.metadata.get("working_dir") == str(project_a.resolve()) and r.id != beta_id]
        listed = [item.id for item in fa.list_directory_sessions()]
        obs.update({"created": created, "beta": beta_id, "project-b": other_dir, "listed": listed, "expected": expected})
        checks.check(f"{label} focus: list_directory_sessions = alpha's sessions of project-a, newest first "
                     "(beta's and project-b's excluded; one with NaN/NUL in other members included)",
                     listed == expected and set(listed) == set(created) | {"focus-odd"}, observed=listed)
        two = [item.id for item in fa.list_directory_sessions(limit=2)]
        checks.check(f"{label} focus: limit=2 returns the newest two", two == expected[:2], observed=two)
        cand = fa.find_candidate_session()
        checks.check(f"{label} focus: find_candidate_session is the newest", cand is not None and cand.id == expected[0],
                     observed=None if cand is None else cand.id)
        beta_list = [item.id for item in focus_runtime(rt, project_a, agent_id="beta").list_directory_sessions()]
        checks.check(f"{label} focus: beta lists only its own project-a session", beta_list == [beta_id], observed=beta_list)
        b_list = [item.id for item in focus_runtime(rt, project_b).list_directory_sessions()]
        checks.check(f"{label} focus: project-b lists its one session", b_list == [other_dir], observed=b_list)
        if setup.db is not None:
            server = setup.db.rows("SELECT count(*) AS n FROM sessions WHERE target = 'focus'")
            obs["postgres focus sessions"] = server
            checks.check(f"{label} focus: the Focus sessions are rows of the PostgreSQL sessions table (independent read)",
                         server == [{"n": 6}], observed=server)
    finally:
        rt.close()


# ---------------------------------------------------------------- Focus CLI in a pseudo-terminal
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[=>()][0-9A-Za-z]?|[\x00-\x08\x0b-\x1f\x7f]")


def focus_cli_section(checks: Checks, setup: Setup, label: str) -> None:
    try:
        import fcntl
        import pty
        import struct
        import termios
    except ImportError:
        NOT_RUN.append({"item": f"{label} focus-cli", "reason": "no pseudo-terminal on this platform"})
        return
    import select

    project = setup.root / "project-cli"
    project.mkdir(parents=True, exist_ok=True)
    before = {r["id"] for r in setup.sessions()}
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    env = clean_env({"TERM": "xterm-256color", "COLUMNS": "120", "LINES": "40"})
    argv = cli_argv(setup.root, setup.cfg_focus_cli, ["--dir", str(project), "--no-update-check", "--no-context"])
    obs: dict[str, Any] = {"argv": ["openminion", *argv]}
    CASES[f"{label} focus-cli"] = obs
    watch = ConnectionWatch(setup.db).__enter__()
    proc = subprocess.Popen([openminion_exe(), *argv], stdin=slave, stdout=slave, stderr=slave, env=env,
                            cwd=str(project), start_new_session=True)
    os.close(slave)
    buf = bytearray()

    def pump(seconds: float, quiet: float | None = None) -> None:
        """Read the terminal until `seconds` pass, the process exits, or (with `quiet`) the
        output has been silent for `quiet` seconds after something was shown."""
        end = time.monotonic() + seconds
        last = time.monotonic()
        while time.monotonic() < end:
            r, _, _ = select.select([master], [], [], 0.2)
            if r:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                buf.extend(chunk)
                last = time.monotonic()
            elif proc.poll() is not None:
                return
            elif quiet is not None and buf and time.monotonic() - last >= quiet:
                return

    steps = []
    pump(120, quiet=10)  # startup: runtime, Focus session, first screen
    steps.append({"after": "startup", "running": proc.poll() is None, "bytes": len(buf)})
    for keys in (b"/exit\r", b"\x04", b"\x03"):
        if proc.poll() is not None:
            break
        os.write(master, keys)
        steps.append({"sent": keys.decode("ascii", "replace")})
        pump(30)
        if proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    if proc.poll() is None:
        proc.kill()
        steps.append({"killed": True})
    rc = proc.wait(timeout=30)
    watch.__exit__(None, None, None)
    os.close(master)
    text = ANSI.sub("", buf.decode("utf-8", "replace"))
    lines = [mask(ln.strip()) for ln in text.splitlines() if ln.strip()]
    obs.update({"steps": steps, "exit_code": rc, "output_tail": lines[-15:]})
    new = [r for r in setup.sessions() if r["id"] not in before]
    news = [{"id": r["id"], "target": r["target"], "metadata": json.loads(str(r["metadata_json"]))} for r in new]
    obs["new sessions"] = news
    checks.check(f"{label} focus-cli: the bare `openminion --dir <project>` Focus launch in a pseudo-terminal starts, "
                 "and `/exit` ends it with exit 0", rc == 0 and not any("killed" in s for s in steps),
                 observed={"exit_code": rc, "steps": steps, "output_tail": lines[-4:]})
    mine = [n for n in news if n["target"] == "focus" and n["metadata"].get("working_dir") == str(project.resolve())]
    checks.check(f"{label} focus-cli: the launch created its Focus directory session (target focus, working_dir = the "
                 "project) in the runtime database", len(mine) == 1, observed=news)
    if setup.db is not None:
        after = setup.db.connections_settled()
        obs["server connections"] = {"max while running": watch.max, "after exit": after}
        checks.check(f"{label} focus-cli: the Focus process connected to PostgreSQL under its application_name while "
                     "running and left no connection after it exited", watch.max > 0 and after["count"] == 0,
                     observed=obs["server connections"])
    if mine:
        rt = setup.runtime()
        try:
            listed = [item.id for item in focus_runtime(rt, project).list_directory_sessions()]
        finally:
            rt.close()
    else:
        listed = None
    if listed is not None:
        obs["listed by the Focus runtime"] = listed
        checks.check(f"{label} focus-cli: the Focus runtime lists that session for the project directory",
                     listed == [mine[0]["id"]], observed=listed)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    ap.add_argument("out")
    ap.add_argument("--backend", default="sqlite", choices=("sqlite", "postgres"))
    args = ap.parse_args()
    install_masking()
    strip_env()
    quiet()
    if args.backend == "postgres" and not pg_base():
        print("PGURL is not set: PostgreSQL not run")
        return 2
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    extra: dict[str, Any] = {"backend": args.backend}
    if args.backend == "postgres":
        extra["postgres"] = pg_versions()
    try:
        import acp  # noqa: F401
        from importlib.metadata import version

        extra["agent-client-protocol"] = version("agent-client-protocol")
    except Exception as exc:  # noqa: BLE001
        extra["agent-client-protocol"] = f"not installed ({type(exc).__name__})"
    info = banner("probe_369", **extra)
    checks = Checks()
    label = args.backend

    @scenario(checks, f"{label} acp")
    def _acp():
        acp_section(checks, work, args.backend, label)

    setup = Setup(work, args.backend, "focus", ("alpha", "beta"))

    @scenario(checks, f"{label} focus")
    def _focus():
        focus_section(checks, setup, label)

    @scenario(checks, f"{label} focus-cli")
    def _focus_cli():
        focus_cli_section(checks, setup, label)

    setup.finish(checks, f"{label} focus")

    summary = checks.summary()
    payload = {"banner": info, "checks": checks.items, "cases": CASES, "not_run": NOT_RUN, "summary": summary}
    write_result(Path(args.out), json.loads(mask(json.dumps(payload, default=str))))
    for item in NOT_RUN:
        print(f"NOT RUN {item['item']}: {item['reason']}")
    print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']} not_run={len(NOT_RUN)}")
    return 0 if checks.all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
