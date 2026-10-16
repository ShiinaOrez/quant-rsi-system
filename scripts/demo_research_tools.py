"""Exercise all six tools over real MCP stdio, without any model/API calls."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def demonstrate(root: Path):
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "scripts.research_server", "--mode", "demo", "--root", str(root)])
    transcript = []
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            definitions = await session.list_tools()
            assert len(definitions.tools) == 6

            async def call(name, arguments):
                result = await session.call_tool(name, arguments)
                value = result.structuredContent
                assert value is not None and value["ok"], value
                transcript.append({"tool": name, "arguments": arguments, "result": value})
                return value["data"]

            found = await call("search_evidence", {"query": "", "kind": "all", "offset": 0, "limit": 10})
            evidence_ids = [e["evidence_id"] for e in found["items"]]
            for evidence_id in evidence_ids:
                await call("read_evidence", {"evidence_id": evidence_id})
            proposal = await call("propose_experiment", {
                "evidence_ids": evidence_ids,
                "hypothesis": "In the synthetic rising-price fixture, full stock exposure should outperform cash after fixed costs.",
                "falsification": "Candidate net return does not exceed control net return on the same two sessions.",
                "parent_candidate_id": "baseline", "control_candidate_id": "baseline", "candidate_kind": "prompt",
                "change_scope": "Change only demo target exposure from zero to one; freeze all accounting and prices.",
                "case_ids": [c["case_id"] for c in found["context"]["cases"]],
                "ablation_plan": "A separate half-exposure experiment could test the exposure effect.",
                "confirmation_plan": "A separate falling-price fixture would test that this is not evidence of market forecasting.",
                "success_criteria": "Compare net_return and max_drawdown; do not promote or infer historical profitability."})
            proposal_id = proposal["proposal"]["proposal_id"]
            candidate = await call("submit_candidate", {"proposal_id": proposal_id, "content": "demo:weight=1", "change_summary": "Full exposure in the explicitly synthetic fixture."})
            queued = await call("request_experiment", {"proposal_id": proposal_id, "candidate_id": candidate["candidate"]["candidate_id"], "idempotency_key": proposal_id + ":paired"})
            attempt_id = queued["attempt"]["attempt_id"]
            for _ in range(50):
                observed = await call("get_experiment", {"attempt_id": attempt_id})
                if observed["attempt"]["status"] in ("completed", "failed"):
                    break
                await asyncio.sleep(0.1)
            assert observed["attempt"]["status"] == "completed", observed
    result = {"execution_mode": "synthetic_demo", "tools_discovered": 6, "attempt_id": attempt_id,
              "status": observed["attempt"]["status"], "promotion": observed["promotion"],
              "budget": observed["budget"], "comparisons": observed["attempt"]["result"]["comparisons"]}
    (root / "mcp-transcript.json").write_text(json.dumps(transcript, indent=2) + "\n")
    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    root = args.root or Path(tempfile.mkdtemp(prefix="quant-research-demo-"))
    root.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(demonstrate(root.resolve()))
    print(json.dumps({"artifacts": str(root), **result}, indent=2))
