"""Shared helpers for the installed-package probes (no repo test fixtures)."""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
from pathlib import Path
from typing import Any

import openminion
from openminion.base.config import AgentProfileConfig, OpenMinionConfig, save_config

OVERRIDE_ENV_KEYS = (
    "OPENMINION_HOME",
    "OPENMINION_DATA_ROOT",
    "OPENMINION_GENERATED_ROOT",
    "OPENMINION_CONFIG_PATH",
)


def banner(name: str) -> dict[str, Any]:
    info = {
        "probe": name,
        "openminion.__file__": openminion.__file__,
        "openminion.__version__": getattr(openminion, "__version__", None),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cwd": os.getcwd(),
        "imported_from_site_packages": "site-packages" in openminion.__file__.replace("\\", "/"),
    }
    print(json.dumps(info, indent=2))
    return info


def quiet() -> None:
    logging.disable(logging.CRITICAL)


def echo_config(
    root: Path,
    *,
    memory_enabled: bool | None = None,
    memory_provider: str | None = None,
) -> Path:
    """The issue reproducer's config: built-in echo provider, explicit storage path."""
    config = OpenMinionConfig()
    config.agents = {
        "openminion": AgentProfileConfig(
            name="openminion", provider="echo", default_channel="console"
        )
    }
    config.default_agent = "openminion"
    config.runtime.process_mode = "single-process"
    config.runtime.daemon_auto_start = False
    if memory_enabled is not None:
        config.runtime.memory_enabled = memory_enabled
    if memory_provider is not None:
        config.runtime.memory_provider = memory_provider
    config.storage.path = str(root / ".openminion" / "state.db")
    root.mkdir(parents=True, exist_ok=True)
    path = root / "agents.json"
    save_config(config, str(path))
    return path


class Checks:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def check(self, name: str, ok: bool, observed: Any = None, expected: Any = None) -> bool:
        item = {"check": name, "pass": bool(ok)}
        if observed is not None:
            item["observed"] = observed
        if expected is not None:
            item["expected"] = expected
        self.items.append(item)
        print(("PASS " if ok else "FAIL ") + name + ("" if observed is None else f" | observed={observed!r}"))
        return bool(ok)

    @property
    def all_pass(self) -> bool:
        return all(item["pass"] for item in self.items)


def write_result(out: Path, payload: dict[str, Any]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
