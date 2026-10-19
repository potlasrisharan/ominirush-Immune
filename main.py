from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from omniimmune.arbiter import Arbiter
from omniimmune.healer.git_branch import GitBranchEngine


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OmniResilience closed-loop chaos healer")
    parser.add_argument("--mode", choices=("docker", "local"), default=os.getenv("OMNI_MODE", "docker"))
    parser.add_argument("--seed", type=int, default=int(os.getenv("OMNI_SEED", "1337")))
    parser.add_argument("--fresh", action="store_true", help="clear cached immune patches before running")
    parser.add_argument("--no-ui", action="store_true", help="disable Rich HUD output")
    parser.add_argument("--json", action="store_true", help="print the final ImmunitySummary as JSON")
    return parser.parse_args(argv)


async def async_main(args: argparse.Namespace) -> int:
    if args.fresh:
        GitBranchEngine().cleanup_heal_branches()
    arbiter = Arbiter(
        target_url=os.getenv("TARGET_URL", "http://127.0.0.1:8000" if args.mode == "local" else "http://target:8000"),
        upstream_url=os.getenv("UPSTREAM_URL", "http://127.0.0.1:9000" if args.mode == "local" else "http://upstream:9000"),
        mode=args.mode,
        seed=args.seed,
        fresh=args.fresh,
        no_ui=args.no_ui or args.json,
    )
    summary = await arbiter.run_closed_loop()
    if args.json:
        print(json.dumps(summary.model_dump(mode="json"), sort_keys=True))
    elif args.no_ui:
        print(f"Immunity score: {summary.score}/100 ({summary.incidents_verified}/{summary.incidents_total} verified)")
    return 0 if summary.score == 100 else 1


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(async_main(parse_args(argv)))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"omniimmune: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
