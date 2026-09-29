#!/usr/bin/env python3
"""Broad end-to-end test across several documents / page selections.

Runs the real pipeline (network + a little money) over a list of documents and prints
per-case and aggregate stats: block types, figures, missing pages, tokens, cost, time.

Usage
-----
  export DS_KEY=...
  PYTHONPATH=backend python3 backend/tools/broad_test.py \
    --case "~/Downloads/a.pdf:2-3:French:judge" \
    --case "~/Downloads/b.pdf:1-2:Chinese (Simplified):tight"

Each ``--case`` is ``path:pages:target_language:figure_mode`` where ``pages`` accepts
``all`` | ``1-5`` | ``2,4,7-9`` and ``figure_mode`` is ``off | tight | judge``.
"""

from __future__ import annotations

import argparse
import os
import time
from collections import Counter

from ocrtran import Pipeline, PipelineConfig

CJK = ("chinese", "japanese", "korean")


def parse_case(value: str):
    parts = value.rsplit(":", 3)
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("case must be path:pages:target:figure_mode")
    path, pages, target, mode = (p.strip() for p in parts)
    if mode not in ("off", "tight", "judge"):
        raise argparse.ArgumentTypeError(f"bad figure_mode: {mode}")
    return os.path.expanduser(path), pages, target, mode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case", action="append", type=parse_case, required=True)
    ap.add_argument("--workdir", default="/tmp/ocrtran_broad")
    ap.add_argument("--api-key-env", default="DS_KEY")
    ap.add_argument("--concurrency", type=int, default=3)
    args = ap.parse_args()

    key = os.environ.get(args.api_key_env)
    if not key:
        raise SystemExit(f"missing ${args.api_key_env}")

    print(
        f"{'document':40s} {'pages':6} {'target':6} {'mode':5} {'ok':2} {'wall':>4} "
        f"{'$':>6} {'calls':>5} {'fig':>3} {'miss':>4}  types"
    )
    total_cost = 0.0
    for i, (path, pages, target, mode) in enumerate(args.case):
        cjk = any(k in target.lower() for k in CJK)
        cfg = PipelineConfig(
            sources=[path],
            workdir=f"{args.workdir}/{i}",
            pages=pages,
            target_lang=target,
            font_main="Noto Sans CJK SC" if cjk else "Noto Serif",
            linebreak_locale="zh" if cjk else "",
            figure_mode=mode,
            concurrency=args.concurrency,
        )
        t0 = time.time()
        try:
            res = Pipeline(cfg, api_key=key).run()
            err = None
        except Exception as exc:  # noqa: BLE001
            res, err = None, str(exc)
        wall = round(time.time() - t0)

        types: Counter = Counter()
        figs = 0
        missing: list = []
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
        if res is not None:
            usage = res.usage or usage
            for entries in res.ocr.values():
                for e in entries:
                    for b in e.get("blocks", []):
                        types[b.get("type")] += 1
                    figs += sum(1 for b in e.get("blocks", []) if b.get("type") == "figure")
            for rep in res.report.values():
                missing += rep.get("missing_pages", [])
        cost = usage.get("prompt_tokens", 0) / 1e6 * 0.28 + usage.get("completion_tokens", 0) / 1e6 * 1.10
        total_cost += cost
        ok = err is None and res is not None and res.ok
        print(
            f"{os.path.basename(path)[:40]:40s} {pages:6} {target[:6]:6} {mode:5} "
            f"{str(ok):2} {wall:4} {cost:6.3f} {usage.get('calls', 0):5} {figs:3} "
            f"{len(missing):4}  {dict(types)}"
        )
        if err:
            print(f"    ERROR: {err}")

    print(f"\n{len(args.case)} cases, approx ${total_cost:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
