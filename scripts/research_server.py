"""Serve Researcher tools over MCP, or process one application-owned queued job."""
import argparse
import asyncio
import json
from pathlib import Path

from quant_rsi.adapters.research_bootstrap import compose
from quant_rsi.tools.mcp_server import serve


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("demo", "local-pilot"))
    parser.add_argument("--root", type=Path, default=Path("artifacts/research"))
    parser.add_argument("--campaign-id", default="research-demo")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--max-evaluations", type=int, default=6)
    parser.add_argument("--work-once", action="store_true")
    args = parser.parse_args()
    service = compose(args.root, args.campaign_id, args.mode, max_attempts=args.max_attempts,
                      max_evaluations=args.max_evaluations)
    if args.work_once:
        print(json.dumps({"processed": service.run_next()}))
    else:
        asyncio.run(serve(service, background_worker=args.mode == "demo"))
