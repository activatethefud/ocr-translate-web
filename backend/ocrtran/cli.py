"""CLI — thin wrapper over the engine (same behavior as the skill)."""

from __future__ import annotations

import argparse
import json
import sys

from .config import ConfigError, PipelineConfig
from .events import Event
from .pipeline import Pipeline


def _print_event(e: Event) -> None:
    loc = f"{e.base}" + (f" p{e.page}" if e.page else "")
    extra = json.dumps(e.data, ensure_ascii=False) if e.data else e.message
    print(f"[{e.stage:9s}] {e.status:8s} {loc:24s} {extra}".rstrip(), flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ocrtran", description=__doc__)
    ap.add_argument("cmd", choices=["ocr", "annot", "build", "assemble", "run"])
    ap.add_argument("--config", required=True)
    ap.add_argument("--api-key", default=None, help="BYOK; else env $DS_KEY")
    ap.add_argument("--force", action="store_true", help="ignore cache")
    args = ap.parse_args(argv)

    try:
        cfg = PipelineConfig.from_json(args.config)
    except (ConfigError, OSError, json.JSONDecodeError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if args.force:
        cfg.force = True

    pipe = Pipeline(cfg, api_key=args.api_key, on_event=_print_event)
    if args.cmd == "run":
        result = pipe.run()
        return 0 if result.ok else 1
    if args.cmd == "ocr":
        pipe.run_ocr()
    elif args.cmd == "annot":
        pipe.run_annotate()
    elif args.cmd == "build":
        pipe.run_build()
    elif args.cmd == "assemble":
        pipe.run_assemble()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
