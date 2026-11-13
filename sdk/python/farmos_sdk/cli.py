"""``farmos`` command-line tool.

    export FARMOS_API_URL=https://farmos.example.com
    export FARMOS_API_KEY=fos_test_...
    farmos balance
    farmos quote http_check '{"url": "https://example.com"}'
    farmos deploy http_check '{"url": "https://example.com"}' --every 300 --runs 12 --max-price 0.01
    farmos deployments
    farmos runs <deployment_id>
    farmos cancel <deployment_id>
    farmos topup 20            # prints a Stripe Checkout URL (test mode unless the operator went live)
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from farmos_sdk.client import FarmosClient, FarmosError


def _json(s: str) -> dict:
    try:
        v = json.loads(s)
    except ValueError as exc:
        raise SystemExit(f"invalid JSON: {exc}")
    if not isinstance(v, dict):
        raise SystemExit("payload must be a JSON object")
    return v


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="farmos", description="FARMOS developer CLI")
    p.add_argument("--url", help="API base URL (default $FARMOS_API_URL)")
    p.add_argument("--key", help="API key (default $FARMOS_API_KEY)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("account")
    sub.add_parser("balance")
    sub.add_parser("topups")
    t = sub.add_parser("topup")
    t.add_argument("amount_usd", type=float)
    q = sub.add_parser("quote")
    q.add_argument("workload_type")
    q.add_argument("payload")
    q.add_argument("--runs", type=int)
    d = sub.add_parser("deploy")
    d.add_argument("workload_type")
    d.add_argument("payload")
    d.add_argument("--name")
    d.add_argument("--every", type=int, help="interval seconds (recurring)")
    d.add_argument("--runs", type=int, help="max runs")
    d.add_argument("--webhook", help="https URL to receive signed results")
    d.add_argument("--max-price", type=float, help="auto-accept the quote up to this USD per run")
    d.add_argument("--quote-id")
    sub.add_parser("deployments")
    r = sub.add_parser("runs")
    r.add_argument("deployment_id")
    c = sub.add_parser("cancel")
    c.add_argument("deployment_id")
    return p


def run(argv: Optional[list[str]] = None, client: Optional[FarmosClient] = None) -> int:
    a = build_parser().parse_args(argv)
    fos = client or FarmosClient(a.url, a.key)
    try:
        if a.cmd == "account":
            out = fos.account()
        elif a.cmd == "balance":
            out = fos.balance()
        elif a.cmd == "topups":
            out = fos.topups()
        elif a.cmd == "topup":
            out = fos.topup(a.amount_usd)
        elif a.cmd == "quote":
            out = fos.quote(a.workload_type, _json(a.payload), runs=a.runs)
        elif a.cmd == "deploy":
            sched = {}
            if a.every:
                sched["interval_seconds"] = a.every
            if a.runs:
                sched["max_runs"] = a.runs
            dest = {"type": "webhook", "url": a.webhook} if a.webhook else {"type": "api"}
            out = fos.create_deployment(a.workload_type, _json(a.payload), name=a.name, schedule=sched,
                                        destination=dest, quote_id=a.quote_id, max_price_usd=a.max_price)
        elif a.cmd == "deployments":
            out = fos.deployments()
        elif a.cmd == "runs":
            out = fos.runs(a.deployment_id)
        elif a.cmd == "cancel":
            out = fos.cancel_deployment(a.deployment_id)
        else:  # pragma: no cover
            raise SystemExit(2)
    except FarmosError as exc:
        print(json.dumps({"error": exc.status_code, "detail": exc.detail}, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2, default=str))
    return 0


def main() -> None:  # pragma: no cover
    sys.exit(run())


if __name__ == "__main__":  # pragma: no cover
    main()
