"""Provider catalog and isolated OpenMinion homes.

Every probe run gets its own OpenMinion home per provider under
``.probe-home/<run_id>/<provider>/``. The home is created with the documented
CLI (``openminion --home-root ... setup --provider ... --no-chat``), so no
provider request is made during setup and no credential is written to disk:
the generated ``agents.json`` only stores the *name* of the key variable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
FAKE_KEY_ENV = "MINIONPROBE_FAKE_API_KEY"
os.environ.setdefault(FAKE_KEY_ENV, "minionprobe-fake-key")  # dummy, local fake provider only


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    preset: str
    model: str
    key_env: str | None
    host: str
    pacing: str = "none"  # none | groq_headers | interval
    min_interval_s: float = 0.0
    base_url: str | None = None
    kind: str = "model"  # model | local
    note: str = ""

    @property
    def agent_id(self) -> str:
        return self.name


PROVIDERS: dict[str, ProviderSpec] = {
    "groq": ProviderSpec(
        name="groq",
        preset="groq",
        model=os.environ.get("MINIONPROBE_GROQ_MODEL", "openai/gpt-oss-120b"),
        key_env="GROQ_API_KEY",
        host="api.groq.com",
        pacing="groq_headers",
        min_interval_s=4.0,
        note="Groq free tier: 8K tokens/min, 1K requests/day per model",
    ),
    "gemini": ProviderSpec(
        name="gemini",
        preset="gemini",
        model=os.environ.get("MINIONPROBE_GEMINI_MODEL", "gemini-3.1-flash-lite"),
        key_env="GEMINI_API_KEY",
        host="generativelanguage.googleapis.com",
        pacing="interval",
        min_interval_s=12.0,
        note="Gemini API free tier via the OpenAI-compatible endpoint",
    ),
    "gemma": ProviderSpec(
        name="gemma",
        preset="gemini",
        model=os.environ.get("MINIONPROBE_GEMMA_MODEL", "gemma-4-31b-it"),
        key_env="GEMINI_API_KEY",
        host="generativelanguage.googleapis.com",
        pacing="interval",
        min_interval_s=12.0,
        note="Gemma 4 served by Google AI Studio (not Cortensor Portal)",
    ),
    "lmstudio": ProviderSpec(
        name="lmstudio",
        preset="custom-openai-compatible",
        model=os.environ.get("MINIONPROBE_LMSTUDIO_MODEL", "local-model"),
        key_env=None,
        host="localhost",
        base_url=os.environ.get("MINIONPROBE_LMSTUDIO_URL", "http://localhost:1234/v1"),
        note="Optional local LM Studio server",
    ),
    "local": ProviderSpec(
        name="local",
        preset="custom-openai-compatible",
        model="fake-model",
        key_env=None,
        host="127.0.0.1",
        kind="local",
        note="MinionProbe's scripted fake provider (offline, deterministic)",
    ),
}


def provider_available(spec: ProviderSpec) -> tuple[bool, str]:
    if spec.kind == "local":
        return True, ""
    if spec.name == "lmstudio":
        import urllib.request

        try:
            urllib.request.urlopen(f"{spec.base_url}/models", timeout=2)  # noqa: S310 - localhost only
            return True, ""
        except Exception:  # noqa: BLE001
            return False, f"LM Studio not reachable at {spec.base_url}"
    if spec.key_env and not os.environ.get(spec.key_env):
        return False, f"{spec.key_env} is not set"
    return True, ""


def openminion_exe() -> list[str]:
    exe = Path(sys.executable).with_name("openminion.exe" if os.name == "nt" else "openminion")
    if exe.exists():
        return [str(exe)]
    return [sys.executable, "-m", "openminion"]


@dataclass
class ProbeHome:
    root: Path
    agents: list[str] = field(default_factory=list)

    @property
    def data_root(self) -> Path:
        return self.root / ".openminion"

    @property
    def config_path(self) -> Path:
        return self.data_root / "agents.json"

    def cli(self, *args: str, timeout: float = 120.0, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        """Run the OpenMinion CLI against this isolated home."""
        cmd = [
            *openminion_exe(),
            "--home-root",
            str(self.root),
            "--data-root",
            str(self.data_root),
            "--no-update-check",
            *args,
        ]
        env = dict(os.environ)
        env.update(isolation_env(self))
        env.setdefault(FAKE_KEY_ENV, "minionprobe-fake-key")
        env["PYTHONIOENCODING"] = "utf-8"
        if extra_env:
            env.update(extra_env)
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            cwd=str(self.root),
        )

    def load_config(self) -> dict[str, Any]:
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def save_config(self, data: dict[str, Any]) -> None:
        self.config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def isolation_env(home: ProbeHome) -> dict[str, str]:
    """Environment that keeps every OpenMinion store inside the probe home.

    ``home_root``/``data_root`` arguments alone are not enough on 0.1.8: the
    identity bridge resolves ``identity.db`` from the environment (see
    issue-drafts/01). Setting both variables is the documented workaround.
    """
    return {"OPENMINION_HOME": str(home.root), "OPENMINION_DATA_ROOT": str(home.data_root)}


def setup_agent(
    home: ProbeHome,
    agent_id: str,
    spec: ProviderSpec,
    *,
    base_url: str | None = None,
    model: str | None = None,
) -> None:
    """Configure one agent profile through ``openminion setup`` (no network)."""
    args = ["setup", "--agent", agent_id, "--provider", spec.preset, "--model", model or spec.model, "--no-chat"]
    url = base_url or spec.base_url
    extra_env: dict[str, str] = {}
    if spec.preset == "custom-openai-compatible":
        args += ["--base-url", str(url), "--api-format", "openai-compatible"]
        # setup insists on the preset's key variable; give it a dummy value
        # and then point the profile at a MinionProbe-specific variable so a
        # real OPENAI_COMPATIBLE_API_KEY is never sent to the fake server.
        extra_env["OPENAI_COMPATIBLE_API_KEY"] = os.environ.get("OPENAI_COMPATIBLE_API_KEY", "minionprobe-dummy")
    proc = home.cli(*args, extra_env=extra_env)
    if proc.returncode != 0:
        raise RuntimeError(f"openminion setup failed for {agent_id}: {proc.stdout[-500:]} {proc.stderr[-500:]}")
    if spec.preset == "custom-openai-compatible" and spec.kind == "local":
        # setup records OPENAI_COMPATIBLE_API_KEY in several places (agent
        # override, providers.openai, system.providers.openai); repoint all of
        # them so the fake provider only ever sees a dummy MinionProbe key.
        text = home.config_path.read_text(encoding="utf-8")
        home.config_path.write_text(text.replace('"OPENAI_COMPATIBLE_API_KEY"', f'"{FAKE_KEY_ENV}"'), encoding="utf-8")
    home.agents.append(agent_id)


def ingest_skill(home: ProbeHome, skill_file: Path, agent_id: str | None = None) -> subprocess.CompletedProcess[str]:
    args = ["skill", "ingest", "--file", str(skill_file), "--scope", "agent" if agent_id else "global", "--trust", "trusted_local"]
    if agent_id:
        args += ["--agent-id", agent_id]
    return home.cli(*args)


def new_home(base: Path, name: str) -> ProbeHome:
    root = base / name
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    return ProbeHome(root=root)
