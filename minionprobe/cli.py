"""Command line entry point: ``python -m minionprobe <command>``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="minionprobe", description="Probe OpenMinion building blocks against real providers.")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="run probes and write results/<date>/run-*.json|md")
    run_p.add_argument("--providers", type=_csv, default=["local", "groq", "gemini"], help="comma list: local,groq,gemini,gemma,lmstudio")
    run_p.add_argument("--blocks", type=_csv, default=["all"], help="comma list of building blocks, or all")
    run_p.add_argument("--only", type=_csv, default=None, help="comma list of probe ids (block.name or name)")
    run_p.add_argument("--out", type=Path, default=None, help="results root (default: ./results)")

    sub.add_parser("list", help="list probes")

    merge_p = sub.add_parser("merge", help="merge run JSON files into one matrix")
    merge_p.add_argument("files", nargs="+", type=Path)
    merge_p.add_argument("--out", type=Path, required=True, help="output path without extension")

    triage_p = sub.add_parser("triage", help="classify failures with an OpenMinion agent and draft issue reports")
    triage_p.add_argument("results", type=Path, help="run or merged JSON file")
    triage_p.add_argument("--provider", default="gemini", help="provider for the triage agent (default gemini)")
    triage_p.add_argument("--max-drafts", type=int, default=3)
    triage_p.add_argument("--offline", action="store_true", help="rule-based triage only (no model calls)")

    args = parser.parse_args(argv)
    if args.command == "list":
        from . import probes  # noqa: F401
        from .core import REGISTRY

        for defn in sorted(REGISTRY.values(), key=lambda d: d.id):
            print(f"{defn.id:45s} scope={defn.scope:5s} turns={defn.cost_turns}  {defn.title}")
        return 0
    if args.command == "run":
        from .env import PROVIDERS
        from .runner import run

        unknown = [p for p in args.providers if p not in PROVIDERS]
        if unknown:
            parser.error(f"unknown providers: {unknown}; choose from {sorted(PROVIDERS)}")
        payload = run(args.providers, args.blocks, args.only, args.out)
        print(f"\nresults: {payload['paths']['json']}\nmatrix:  {payload['paths']['md']}")
        return 0
    if args.command == "merge":
        from .runner import merge

        merge(args.files, args.out)
        print(f"merged into {args.out.with_suffix('.md')}")
        return 0
    if args.command == "triage":
        from .triage import triage

        out = triage(args.results, provider=args.provider, max_drafts=args.max_drafts, offline=args.offline)
        print(f"triage written to {out}")
        return 0
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
