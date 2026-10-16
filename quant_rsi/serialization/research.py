from dataclasses import asdict
import json

from quant_rsi.domain.experiments import BenchmarkCase, BenchmarkIdentity
from quant_rsi.domain.research import Attempt, AttemptStatus, Candidate, Evidence, Proposal, ResearchCampaign, ResearchCase, check
from quant_rsi.domain.shared import DatasetSplit


def encode(campaign):
    value = asdict(campaign)
    value["inspected_ids"] = sorted(campaign.inspected_ids)
    return json.dumps({"schema_version": "research-state.v1", "campaign": value}, sort_keys=True, allow_nan=False)


def decode(raw):
    envelope = json.loads(raw)
    check(envelope.get("schema_version") == "research-state.v1", "unsupported_state_schema", "Unsupported research state format.")
    value = envelope["campaign"]
    value["supported_kinds"] = tuple(value["supported_kinds"])
    value["inspected_ids"] = set(value["inspected_ids"])
    for key, evidence in value["evidence"].items():
        evidence["split"] = DatasetSplit(evidence["split"]) if evidence["split"] else None
        value["evidence"][key] = Evidence(**evidence)
    for key, binding in value["cases"].items():
        case = binding["case"]
        case["benchmark"] = BenchmarkIdentity(**case["benchmark"])
        case["split"] = DatasetSplit(case["split"])
        value["cases"][key] = ResearchCase(BenchmarkCase(**case), binding["ready"])
    for key, candidate in value["candidates"].items():
        value["candidates"][key] = Candidate(**candidate)
    for key, proposal in value["proposals"].items():
        proposal["evidence_ids"] = tuple(proposal["evidence_ids"])
        proposal["case_ids"] = tuple(proposal["case_ids"])
        value["proposals"][key] = Proposal(**proposal)
    for key, attempt in value["attempts"].items():
        attempt["status"] = AttemptStatus(attempt["status"])
        value["attempts"][key] = Attempt(**attempt)
    return ResearchCampaign(**value)
