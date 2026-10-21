"""Run probes per provider and write results/<date>/."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import platform
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import probes  # noqa: F401 - registers probes
from .core import REGISTRY, ProbeContext, ProbeDef, ProbeResult, execute
from .env import (
    PROVIDERS,
    REPO_ROOT,
    ProbeHome,
    ProviderSpec,
    ingest_and_admit,
    ingest_skill,
    isolation_env,
    json_tail,
    new_home,
    provider_available,
    setup_agent,
)
from .fakeprovider import FakeProvider, FakeScript
from .recorder import RECORDER

SKILL_TEMPLATE = """# Purpose

Answer questions about the MinionProbe release codename.

# Metadata

1. `skill_id`: `minionprobe.release_codename`
2. `risk_level`: `low`
3. `channels`: `console`
4. `required_tools`: none
5. `approval_mode`: `auto`

# Inputs

1. `question` (string): a question about the MinionProbe release codename.

# Recipe

1. When the user asks for the MinionProbe release codename, answer exactly: `{codename}`.
2. Do not call any tool.

# Expected Output

1. Plain text containing `{codename}`.

# Security Notes

1. The codename is public test data; it is not a secret.

# Test Cases

1. Input: `What is the MinionProbe release codename?` -> output contains `{codename}`
"""


def _quiet_logging() -> None:
    logging.basicConfig(level=logging.WARNING)
    for name in ("openminion", "httpx", "alembic", "sophiagraph"):
        logging.getLogger(name).setLevel(logging.ERROR)


def select_probes(blocks: list[str] | None, only: list[str] | None, scope: str) -> list[ProbeDef]:
    selected = []
    for defn in REGISTRY.values():
        if defn.scope != scope:
            continue
        if blocks and "all" not in blocks and defn.block not in blocks:
            continue
        if only and defn.id not in only and defn.id.split(".")[-1] not in only:
            continue
        selected.append(defn)
    # basic_turn first: routing and stats probes reuse its evidence
    selected.sort(key=lambda d: (d.id != "agent_execution.basic_turn", d.cost_turns == 0, d.id))
    return selected


def _prepare_model_home(base: Path, spec: ProviderSpec, codename: str) -> tuple[ProbeHome, dict[str, Any]]:
    home = new_home(base, spec.name)
    setup_agent(home, spec.agent_id, spec)
    skill_file = home.root / "skills" / "release-codename" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(SKILL_TEMPLATE.format(codename=codename), encoding="utf-8")
    # 1) the documented default: ingest the file as-is
    default = json_tail(ingest_skill(home, skill_file).stdout)
    # 2) the working path we found: explicit --name, then admit as verified
    admitted = ingest_and_admit(home, skill_file, "minionprobe.release_codename")
    listing = home.cli("skill", "list")
    skill_info = {
        "codename": codename,
        "default_ok": bool(default.get("ok")),
        "default_skill_id": default.get("skill_id"),
        "default_warnings": default.get("warnings", []),
        "named_skill_id": admitted["skill_id"],
        "admit_ok": admitted["admit_ok"],
        "admit_error": admitted["admit_error"],
        "listed": "release_codename" in listing.stdout,
        "list_output": listing.stdout.strip()[-300:],
    }
    return home, skill_info


def _prepare_local_home(base: Path, fake: FakeProvider) -> ProbeHome:
    home = new_home(base, "local")
    local = PROVIDERS["local"]
    setup_agent(home, "local", local, base_url=fake.base_url)
    setup_agent(home, "fake-a", local, base_url=fake.base_url, model="model-a")
    setup_agent(home, "fake-b", local, base_url=fake.base_url, model="model-b")
    setup_agent(home, "broken", local, base_url="http://127.0.0.1:9/v1", model="nobody-home")
    return home


def _runtime_factory(home: ProbeHome) -> Callable[[], Any]:
    def build() -> Any:
        from openminion import APIRuntime

        return APIRuntime.from_config_path(str(home.config_path), home_root=home.root, data_root=home.data_root)

    return build


def run(
    providers: list[str],
    blocks: list[str] | None = None,
    only: list[str] | None = None,
    out_root: Path | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    _quiet_logging()
    import openminion

    run_id = uuid.uuid4().hex[:6]
    started = dt.datetime.now().astimezone()
    out_dir = (out_root or REPO_ROOT / "results") / started.strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    homes_base = REPO_ROOT / ".probe-home" / f"{started.strftime('%Y%m%d-%H%M%S')}-{run_id}"
    RECORDER.install()
    results: list[ProbeResult] = []
    provider_meta: dict[str, Any] = {}
    saved_env = {k: os.environ.get(k) for k in ("OPENMINION_HOME", "OPENMINION_DATA_ROOT")}
    try:
        for name in providers:
            spec = PROVIDERS[name]
            available, reason = provider_available(spec)
            scope = "local" if spec.kind == "local" else "model"
            defs = select_probes(blocks, only, scope)
            provider_meta[name] = {"model": spec.model, "host": spec.host, "note": spec.note, "available": available, "reason": reason}
            if not available:
                log(f"[{name}] skipped: {reason}")
                continue
            if not defs:
                continue
            fake = FakeProvider(FakeScript()).start() if spec.kind == "local" else None
            try:
                extras: dict[str, Any] = {}
                if fake is not None:
                    home = _prepare_local_home(homes_base, fake)
                    extras["fake"] = fake
                else:
                    home, extras["skill"] = _prepare_model_home(homes_base, spec, f"CODENAME-{uuid.uuid4().hex[:6].upper()}")
                os.environ.update(isolation_env(home))
                ctx = ProbeContext(
                    provider=spec, home=home, run_id=run_id, out_dir=out_dir,
                    runtime_factory=_runtime_factory(home), log=log, extras=extras,
                )
                log(f"[{name}] {len(defs)} probes, model={spec.model}, home={home.root}")
                try:
                    for defn in defs:
                        log(f"  - {defn.id} ...")
                        result = execute(defn, ctx)
                        results.append(result)
                        log(f"    {result.status} ({result.latency_s:.1f}s) {result.summary[:140]}")
                        _write(out_dir, run_id, started, providers, provider_meta, results, openminion.__version__)
                finally:
                    ctx.close()
            finally:
                if fake is not None:
                    fake.stop()
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return _write(out_dir, run_id, started, providers, provider_meta, results, openminion.__version__)


def _write(
    out_dir: Path,
    run_id: str,
    started: dt.datetime,
    providers: list[str],
    provider_meta: dict[str, Any],
    results: list[ProbeResult],
    om_version: str,
) -> dict[str, Any]:
    from .report import render_matrix

    payload = {
        "tool": "minionprobe",
        "run_id": run_id,
        "started": started.isoformat(timespec="seconds"),
        "finished": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "openminion_version": om_version,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "providers": provider_meta,
        "results": [r.to_json() for r in results],
    }
    stem = f"run-{started.strftime('%H%M%S')}-{run_id}"
    (out_dir / f"{stem}.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    (out_dir / f"{stem}.md").write_text(render_matrix(payload), encoding="utf-8")
    payload["paths"] = {"json": str(out_dir / f"{stem}.json"), "md": str(out_dir / f"{stem}.md")}
    return payload


def merge(paths: list[Path], out: Path) -> dict[str, Any]:
    """Merge several run JSON files (latest result per provider+probe wins)."""
    from .report import render_matrix

    merged: dict[str, Any] = {"tool": "minionprobe", "runs": [], "providers": {}, "results": []}
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for path in sorted(paths):
        data = json.loads(path.read_text(encoding="utf-8"))
        merged["runs"].append({"file": path.name, "run_id": data["run_id"], "started": data["started"], "openminion_version": data["openminion_version"]})
        for name, meta in data["providers"].items():
            if meta.get("available"):
                merged["providers"][name] = meta
        for result in data["results"]:
            latest[(result["provider"], result["probe"])] = result | {"run_id": data["run_id"]}
        merged.update({k: data[k] for k in ("openminion_version", "python", "platform")})
    merged["results"] = list(latest.values())
    merged["started"] = merged["runs"][0]["started"] if merged["runs"] else ""
    merged["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    out.with_suffix(".json").write_text(json.dumps(merged, indent=2), encoding="utf-8")
    out.with_suffix(".md").write_text(render_matrix(merged), encoding="utf-8")
    return merged
