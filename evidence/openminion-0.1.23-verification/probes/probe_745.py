"""#745 published-package check: the published README `lookup_order` snippet under the default action policy.

Installed package only (no repo test fixtures); run with `python -I -X utf8` from
an empty directory outside any checkout.

Input: the published README (the PyPI project description of the version under
test, byte-identical to the tag's README.md; see release-source/). The probe
takes the README's Python block that defines `lookup_order` and executes the
README's own imports and function definition (decorator included) against the
installed package. It reads the snippet's `instructions`, `session_id` and run
message from the same block, so nothing is retyped.

Cases, each with a fresh runtime home and the default `action_policy` from a
default `OpenMinionConfig()`; the `openai` provider is a scripted
OpenAI-compatible server on 127.0.0.1 (the #448 probe's helpers,
`probe_420_lib.py` and `_common.py`, unchanged). The scripted model calls
`lookup_order` once with {"order_id": "A-104"}; no `approval_callback` is passed,
as in the README:
  README      the published declaration, as written in the README;
  UNDECLARED  the same README function with the decorator replaced by a bare
              `@tool` (the undeclared negative control).

Public SDK path: `Agent(instructions=..., tools=[fn], session_id=...,
runtime=runtime).run(message)`. `runtime=` is the documented way to pass an
`APIRuntime`; the README itself uses the default config, which would need a real
provider.

"Executes" is observed at the provider: the request after the tool call carries a
`tool` message whose content is the README function's return value.

Also: the installed `openminion.tool` docstring contains the published
correction (qualified statements below).

Exit code: 0 only if every EXPECT check passes.

Usage: python -I -X utf8 probe_745.py <work_dir> <out.json> <published_readme.md>
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import sys
import time
import traceback
from importlib import metadata
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, write_result  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
README = Path(sys.argv[3]).resolve()
WORK.mkdir(parents=True, exist_ok=True)
quiet()
clear_override_env()


def _dist(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


info = banner("probe_745",
              provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1",
              dist_versions={n: _dist(n) for n in ("openminion", "pydantic")})

import openminion  # noqa: E402
from openminion import Agent  # noqa: E402

import probe_420_lib as lib  # noqa: E402
from probe_420_lib import ScriptedProvider, build_runtime, chdir_env, restore_env  # noqa: E402

TOOL = "lookup_order"
ARGS = {"order_id": "A-104"}
PROMPT_MARK = "policy confirmation required"
expect = Checks()


# ---------------------------------------------------------------- the published snippet
def readme_block(text: str) -> str:
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.S)
    found = [b for b in blocks if re.search(rf"^def {TOOL}\(", b, flags=re.M)]
    if len(found) != 1:
        raise SystemExit(f"expected one README python block defining {TOOL}, found {len(found)}")
    return found[0]


readme_bytes = README.read_bytes()
block = readme_block(readme_bytes.decode("utf-8"))
tree = ast.parse(block)
imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == TOOL)
agent_call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "Agent")
agent_kwargs = {k.arg: ast.literal_eval(k.value) for k in agent_call.keywords if k.arg in ("instructions", "session_id")}
run_call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "run")
message = ast.literal_eval(run_call.args[0])
callback_in_snippet = any(k.arg == "approval_callback" for k in run_call.keywords)
decorator_src = [ast.unparse(d) for d in func.decorator_list]


def build_tool(*, bare: bool) -> tuple[Any, str]:
    node = ast.parse(ast.unparse(func)).body[0]
    if bare:
        node.decorator_list = [ast.Name(id="tool", ctx=ast.Load())]
    module = ast.Module(body=[*imports, node], type_ignores=[])
    source = ast.unparse(module)
    namespace: dict[str, Any] = {"__name__": "readme_snippet"}
    exec(compile(source, "<published README snippet>", "exec"), namespace)  # noqa: S102 - the README's own code
    return namespace[TOOL], source


snippet = {"readme_sha256": __import__("hashlib").sha256(readme_bytes).hexdigest(), "block": block,
           "decorators": decorator_src, "agent_kwargs": agent_kwargs, "message": message,
           "approval_callback_in_snippet": callback_in_snippet}


# ---------------------------------------------------------------- scripted model
class ToolCallProvider(ScriptedProvider):
    """The #448 probe's scripted server, plus one call to the armed tool and a record of tool results."""

    def __init__(self) -> None:
        self.armed: str | None = None
        self.tool_results: list[dict[str, Any]] = []
        super().__init__()

    def respond(self, body: dict[str, Any], path: str) -> dict[str, Any]:
        names = [str((t.get("function") or {}).get("name", "")) for t in body.get("tools") or [] if isinstance(t, dict)]
        choice = body.get("tool_choice")
        forced = str((choice.get("function") or {}).get("name", "")) if isinstance(choice, dict) else ""
        match = None
        with self.lock:
            for m in body.get("messages") or []:
                if isinstance(m, dict) and m.get("role") == "tool":
                    self.tool_results.append({"tool_call_id": m.get("tool_call_id"), "content": m.get("content")})
            if forced != "submit_output" and self.armed and TOOL in names:
                match = TOOL
                self.armed = None
                index = len(self.requests)
                self.requests.append({"index": index, "kind": "tool_call", "path": path, "t": time.monotonic(),
                                      "tool": match})
        if match:
            call = {"id": f"call-{match}-{index}", "type": "function",
                    "function": {"name": match, "arguments": json.dumps(ARGS)}}
            return lib._completion({"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls")
        return super().respond(body, path)


class PolicyRecorder:
    """Call-through recorder on the runtime's action policy controller (`check`)."""

    def __init__(self, policy: Any) -> None:
        self.policy = policy
        self.rows: list[dict[str, Any]] = []
        original = policy.check

        def check(invocation: Any, ctx: Any, *args: Any, **kwargs: Any) -> Any:
            decision = original(invocation, ctx, *args, **kwargs)
            inv = invocation if isinstance(invocation, dict) else {}
            key = f"{inv.get('tool', '')}.{inv.get('method', '')}"
            if TOOL in key:
                risk = getattr(decision, "risk", None)
                self.rows.append({"key": key, "decision": getattr(decision, "decision", None),
                                  "reason_code": getattr(decision, "reason_code", None),
                                  "risk": risk.to_dict() if hasattr(risk, "to_dict") else repr(risk)})
            return decision

        policy.check = check

    def uninstall(self) -> None:
        try:
            delattr(self.policy, "check")
        except AttributeError:
            pass


def run_case(label: str, *, bare: bool) -> dict[str, Any]:
    root = WORK / label
    provider = ToolCallProvider()
    old_env = chdir_env(root)
    row: dict[str, Any] = {"case": label}
    runtime = rec = None
    try:
        fn, source = build_tool(bare=bare)
        row["executed_source"] = source
        d = fn.tool_decl
        row["tool_decl"] = {"name": d.name, "min_scope": d.min_scope, "idempotent": d.idempotent,
                            "dangerous": d.dangerous, "tags": list(d.tags), "capabilities": list(d.capabilities)}
        runtime = build_runtime(root, provider)
        policy = getattr(runtime, "action_policy", None)
        row["action_policy_present"] = policy is not None
        if policy is not None:
            rec = PolicyRecorder(policy)
        provider.armed = TOOL
        try:
            with Agent(tools=[fn], runtime=runtime, **agent_kwargs) as agent:
                res = agent.run(message, timeout_seconds=120)
            row.update({"run_state": res.run_state, "text": res.text,
                        "respond_kind": (res.metadata or {}).get("respond_kind")})
        except Exception as exc:  # noqa: BLE001 - recorded
            row["error"] = f"{type(exc).__name__}: {exc}"
        row["model_called_tool"] = any(r.get("kind") == "tool_call" for r in provider.requests)
        row["provider_requests"] = [r["kind"] for r in provider.requests]
        row["tool_results_seen_by_model"] = provider.tool_results
        row["provider_hosts"] = sorted({provider.base_url.split("/")[2].split(":")[0]})
    except Exception as exc:  # noqa: BLE001 - recorded; the checks then fail
        row["case_error"] = f"{type(exc).__name__}: {exc}"
        row["case_error_frames"] = [f"{Path(f.filename).name}:{f.lineno} {f.name}"
                                    for f in traceback.extract_tb(exc.__traceback__)][-8:]
    finally:
        row["policy_checks"] = rec.rows if rec is not None else []
        if rec is not None:
            rec.uninstall()
        if runtime is not None:
            try:
                runtime.close()
            except Exception as exc:  # noqa: BLE001
                row["runtime_close"] = f"{type(exc).__name__}: {exc}"
        restore_env(old_env)
        provider.close()
    return row


cases = {"README": run_case("README", bare=False), "UNDECLARED": run_case("UNDECLARED", bare=True)}
EXPECTED_RESULT = f"order {ARGS['order_id']}: ready"


def _tool_data(content: Any) -> Any:
    """The runtime sends a tool result as a JSON envelope; the function's return value is outputs.data."""
    try:
        envelope = json.loads(str(content))
    except (TypeError, ValueError):
        return None
    if not isinstance(envelope, dict) or envelope.get("status") != "success":
        return None
    return (envelope.get("outputs") or {}).get("data")


def executed(row: dict[str, Any]) -> bool:
    return any(_tool_data(t.get("content")) == EXPECTED_RESULT for t in row.get("tool_results_seen_by_model") or [])


def prompted(row: dict[str, Any]) -> bool:
    return PROMPT_MARK in str(row.get("text") or "").lower() or row.get("respond_kind") == "policy_confirmation_prompt"


def first(row: dict[str, Any]) -> dict[str, Any]:
    return (row.get("policy_checks") or [{}])[0]


def facts(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row.get(k) for k in ("tool_decl", "run_state", "respond_kind", "text", "error", "case_error",
                                    "model_called_tool", "tool_results_seen_by_model", "policy_checks")}


r, u = cases["README"], cases["UNDECLARED"]
expect.check("EXPECT Published README snippet declares lookup_order @tool(min_scope=\"READ_ONLY\", idempotent=True) "
             "and passes no approval_callback",
             decorator_src == ["tool(min_scope='READ_ONLY', idempotent=True)"] and not callback_in_snippet, snippet)
expect.check("EXPECT README (published declaration, default policy, no approval_callback): policy ALLOW / "
             "READ_ONLY_ALLOW on a read, the lookup executes (its return value reaches the model) and the reply is "
             "not a confirmation prompt",
             r.get("model_called_tool") and executed(r) and not prompted(r)
             and first(r).get("decision") == "ALLOW" and first(r).get("reason_code") == "READ_ONLY_ALLOW"
             and (first(r).get("risk") or {}).get("risk_class") == "read",
             facts(r))
expect.check("EXPECT UNDECLARED negative control (same README function, bare @tool, no approval_callback): "
             "REQUIRE_CONFIRM / UNKNOWN_REVERSIBILITY, the reply is the confirmation prompt and the lookup does not execute",
             u.get("model_called_tool") and prompted(u) and not executed(u)
             and first(u).get("decision") == "REQUIRE_CONFIRM" and first(u).get("reason_code") == "UNKNOWN_REVERSIBILITY",
             facts(u))
hosts = sorted({h for row in cases.values() for h in row.get("provider_hosts", [])})
errors = {k: row.get("case_error") or row.get("error") for k, row in cases.items() if row.get("case_error") or row.get("error")}
expect.check("EXPECT Offline and error-free: every case's provider is the scripted server on 127.0.0.1 and no case raised",
             hosts == ["127.0.0.1"] and not errors, {"hosts": hosts, "errors": errors})

doc = inspect.getdoc(openminion.tool) or ""
doc_flat = " ".join(doc.split())
PHRASES = [
    "Without ``min_scope`` the tool is ``WRITE_SAFE``",
    "``min_scope=\"READ_ONLY\"`` classifies it as a read with no side effects. The default policy then runs it without confirmation. Explicit denials, sensitive-target handling and operator policy still apply.",
    "``idempotent=True`` only marks a write as reversible.",
    "``dangerous=True`` classifies the tool as destructive and irreversible, which requires confirmation under a fresh default configuration. A matching grant or an explicit policy rule can still resolve it first.",
    "``tags`` and ``capabilities`` do not declare risk",
]
missing = [p for p in PHRASES if p not in doc_flat]
expect.check("EXPECT Installed openminion.tool docstring carries the published @tool risk mapping (qualified wording)",
             not missing, {"missing": missing, "module_file": inspect.getsourcefile(openminion.tool)})

summary = expect.summary()
write_result(OUT, {"info": info, "summary": summary, "all_pass": expect.all_pass, "checks": expect.items,
                   "observed": {"snippet": snippet, "cases": cases, "tool_docstring": doc}})
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if expect.all_pass else 1)
