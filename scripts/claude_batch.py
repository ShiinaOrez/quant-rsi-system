"""Run one subscription Claude job with a local lock and usage admission check.

This is a conservative admission rule, not a provider-enforced hard token cap.
Unknown usage blocks dispatch. No paid credits or plan changes are requested.
"""
import argparse
import fcntl
import json
from pathlib import Path
import re
import subprocess
import time


def parse_usage(result: str) -> dict:
    labels = {"session": "Current session", "week": "Current week (all models)"}
    usage = {}
    for name, label in labels.items():
        match = re.search(re.escape(label) + r":\s*(\d+(?:\.\d+)?)% used[^\n]*", result)
        if not match:
            raise ValueError(f"cannot determine {name} quota; dispatch blocked")
        percent = float(match.group(1))
        if not 0 <= percent <= 100:
            raise ValueError("invalid usage percentage")
        usage[name] = {"used_percent": percent, "display": match.group(0)}
    for match in re.finditer(r"(Current week \([^\n:]+\)):\s*(\d+(?:\.\d+)?)% used[^\n]*", result):
        if match.group(1) == labels["week"]:
            continue
        percent = float(match.group(2))
        if not 0 <= percent <= 100:
            raise ValueError("invalid model-specific usage percentage")
        usage[match.group(1)] = {"used_percent": percent, "display": match.group(0)}
    return usage


def read_usage(cli: str, output: Path) -> dict:
    command = [cli, "--safe-mode", "--restricted", "-p", "--tools", "",
               "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--no-session-persistence", "--output-format", "json", "/usage"]
    response = subprocess.run(command, capture_output=True, text=True, timeout=90, check=True)
    raw = json.loads(response.stdout)
    if raw.get("is_error") or raw.get("subtype") != "success":
        raise ValueError("Claude usage query failed; dispatch blocked")
    usage = parse_usage(raw.get("result", ""))
    output.write_text(json.dumps({"checked_at": time.time(), "usage": usage}, indent=2) + "\n")
    return usage


def run(args):
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", args.name):
        raise ValueError("invalid batch name")
    with (root / "dispatch.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state_path = root / "dispatch-state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        remaining = state.get("finished_at", 0) + args.interval - time.time()
        if remaining > 0:
            raise ValueError(f"cooldown active: retry in {int(remaining) + 1}s")
        batch = root / args.name
        batch.mkdir(exist_ok=True)
        if (batch / "response.json").exists():
            raise FileExistsError("response already exists; use a new batch name")
        usage = read_usage(args.cli, batch / "usage-before.json")
        if (usage["session"]["used_percent"] >= args.session_stop
                or any(item["used_percent"] >= args.week_stop
                       for name, item in usage.items() if name != "session")):
            raise ValueError("quota reserve reached; wait for provider reset before dispatch")
        prompt = args.request.read_bytes()
        (batch / "request.md").write_bytes(prompt)
        command = [args.cli, "--safe-mode", "--restricted", "-p", "--model", "claude-opus-5-5",
                   "--effort", "high", "--tools", "Read,Write,Edit,Bash,WebSearch,WebFetch",
                   "--allowedTools", "Read,Write,Edit,Bash,WebSearch,WebFetch",
                   "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                   "--permission-mode", "dontAsk", "--max-turns", str(args.max_turns),
                   "--output-format", "json", "--no-session-persistence"]
        started = time.time()
        exit_code = None
        try:
            with (batch / "response.json").open("wb") as output, (batch / "stderr.log").open("wb") as error:
                process = subprocess.run(command, input=prompt, stdout=output, stderr=error,
                                         timeout=args.timeout)
                exit_code = process.returncode
        finally:
            receipt = {"started_at": started, "finished_at": time.time(), "exit_code": exit_code,
                       "model": "claude-opus-5-5", "max_turns": args.max_turns,
                       "timeout_seconds": args.timeout, "batch": args.name}
            try:
                response = json.loads((batch / "response.json").read_text())
                receipt["reported_num_turns"] = response.get("num_turns")
                receipt["result_subtype"] = response.get("subtype")
                receipt["is_error"] = response.get("is_error")
                receipt["models_used"] = sorted(response.get("modelUsage", {}))
            except (ValueError, OSError):
                receipt["response_parse_failed"] = True
            (batch / "dispatch.json").write_text(json.dumps(receipt, indent=2) + "\n")
            state_path.write_text(json.dumps(receipt, indent=2) + "\n")
            try:
                after = read_usage(args.cli, batch / "usage-after.json")
                print(json.dumps({"batch": args.name, "exit_code": exit_code, "usage_after": after}), flush=True)
            except Exception as error:
                # Preserve both the original job outcome and a failed quota check.
                (batch / "usage-after-error.txt").write_text(type(error).__name__ + "\n")
        if exit_code:
            raise RuntimeError(f"Claude exited with code {exit_code}; inspect saved response")
        if (receipt.get("result_subtype") != "success" or receipt.get("is_error")
                or "claude-opus-5-5" not in receipt.get("models_used", [])):
            raise RuntimeError("Claude result incomplete or requested model unverified; inspect saved response")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--root", type=Path, default=Path("artifacts/benchmark-quality/claude"))
    parser.add_argument("--interval", type=int, default=300, help="minimum seconds after previous batch finishes")
    parser.add_argument("--max-turns", type=int, choices=range(1, 33), default=24)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--session-stop", type=float, default=65)
    parser.add_argument("--week-stop", type=float, default=70)
    args = parser.parse_args()
    if args.interval < 300 or not 0 < args.timeout <= 1800:
        parser.error("require >=300s cooldown and <=1800s job timeout")
    if not 0 < args.session_stop <= 65 or not 0 < args.week_stop <= 70:
        parser.error("quota reserve cannot be reduced below defaults")
    run(args)
