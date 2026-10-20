from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

DEFAULT_DASHBOARD_PORT = 8080


def _port_number(value: str) -> int:
    """Parse a TCP port for the dashboard CLI option."""
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OmniResilience closed-loop chaos healer")
    parser.add_argument("--version", action="version", version="omniimmune 1.0.0")
    parser.add_argument("--mode", choices=("docker", "local"), default=os.getenv("OMNI_MODE", "docker"))
    parser.add_argument("--seed", type=int, default=int(os.getenv("OMNI_SEED", "1337")))
    parser.add_argument("--fresh", action="store_true", help="clear cached immune patches before running")
    parser.add_argument("--no-ui", action="store_true", help="disable Rich HUD output")
    parser.add_argument("--json", action="store_true", help="print the final ImmunitySummary as JSON")
    parser.add_argument(
        "--push-pr",
        action="store_true",
        help="automatically push verified heal branches and create GitHub Pull Requests via gh CLI",
    )
    parser.add_argument(
        "--serve-dashboard",
        nargs="?",
        const=DEFAULT_DASHBOARD_PORT,
        default=None,
        type=_port_number,
        metavar="PORT",
        help=f"serve the browser dashboard without running chaos (default port: {DEFAULT_DASHBOARD_PORT})",
    )
    return parser.parse_args(argv)


async def async_main(args: argparse.Namespace) -> int:
    from omniimmune.arbiter import Arbiter
    from omniimmune.healer.git_branch import GitBranchEngine

    if args.fresh:
        GitBranchEngine().cleanup_heal_branches()
    arbiter = Arbiter(
        target_url=os.getenv("TARGET_URL", "http://127.0.0.1:8000" if args.mode == "local" else "http://target:8000"),
        upstream_url=os.getenv("UPSTREAM_URL", "http://127.0.0.1:9000" if args.mode == "local" else "http://upstream:9000"),
        mode=args.mode,
        seed=args.seed,
        fresh=args.fresh,
        no_ui=args.no_ui or args.json,
        push_pr=args.push_pr,
    )
    summary = await arbiter.run_closed_loop()
    if args.json:
        print(json.dumps(summary.model_dump(mode="json"), sort_keys=True))
    elif args.no_ui:
        print(f"Immunity score: {summary.score}/100 ({summary.incidents_verified}/{summary.incidents_total} verified)")
    if args.push_pr and any(incident.get("pr_status") == "FAILED" for incident in summary.incidents):
        return 1
    return 0 if summary.score == 100 else 1


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        if args.serve_dashboard is not None:
            from omniimmune.ui.web import DashboardStore, create_dashboard_app
            import uvicorn

            state_path = Path(
                os.getenv(
                    "OMNI_DASHBOARD_STATE_PATH",
                    str(Path(__file__).resolve().parent / "artifacts" / "dashboard_state.json"),
                )
            )
            store = DashboardStore()
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                store.replace(payload)
            uvicorn.run(
                create_dashboard_app(store),
                host="127.0.0.1",
                port=args.serve_dashboard,
                log_level="info",
            )
            return 0
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"omniimmune: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
