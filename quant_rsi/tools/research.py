"""One schema catalog drives discovery, runtime validation and MCP export."""
from copy import deepcopy
from importlib.resources import files
import json

from jsonschema import Draft202012Validator

from quant_rsi.application.recording import wire
from quant_rsi.domain.research import ResearchError


def load_catalog():
    return json.loads(files("quant_rsi.tools").joinpath("research-tools.v1.json").read_text())


class ResearchTools:
    def __init__(self, service):
        self.service, self.catalog = service, load_catalog()
        self.definitions = {t["name"]: t for t in self.catalog["tools"]}
        for definition in self.definitions.values():
            for field in ("inputSchema", "outputSchema"):
                Draft202012Validator.check_schema(definition[field])

    def list_tools(self):
        return deepcopy(self.catalog["tools"])

    @staticmethod
    def action(name, reason, **arguments):
        return {"tool": name, "reason": reason, "arguments": arguments}

    def call(self, name, arguments):
        data, error, next_actions = None, None, []
        try:
            if name not in self.definitions:
                raise ResearchError("unknown_tool", "Use one of the six registered research tools.")
            try:
                json.dumps(arguments, allow_nan=False)
                valid = Draft202012Validator(self.definitions[name]["inputSchema"]).is_valid(arguments)
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise ResearchError("invalid_arguments", "Arguments do not match inputSchema; check required fields, types and unknown properties.")
            data = wire(self.service.execute(name, arguments))
            if name == "search_evidence":
                next_actions = [self.action("read_evidence", "Read relevant evidence before citing it.")]
            elif name == "read_evidence":
                next_actions = [self.action("propose_experiment", "After reading all cited items, define a falsifiable paired experiment.")]
            elif name == "propose_experiment":
                next_actions = [self.action("submit_candidate", "Implement the proposed component as a complete artifact.", proposal_id=data["proposal"]["proposal_id"])]
            elif name == "submit_candidate":
                candidate = data["candidate"]
                next_actions = [self.action("request_experiment", "Queue paired evaluation with one stable idempotency key.",
                                            proposal_id=candidate["proposal_id"], candidate_id=candidate["candidate_id"])]
            elif name in ("request_experiment", "get_experiment"):
                attempt = data["attempt"]
                if attempt["status"] in ("queued", "running"):
                    next_actions = [self.action("get_experiment", "Wait before polling; do not queue a duplicate run.", attempt_id=attempt["attempt_id"])]
                else:
                    next_actions = [self.action("read_evidence", "Read the recorded outcome before citing it in a follow-up hypothesis.",
                                                evidence_id="result-" + attempt["attempt_id"])]
        except ResearchError as failure:
            error = {"code": failure.code, "message": str(failure), "retryable": False}
            target = "read_evidence" if failure.code == "evidence_not_read" else "search_evidence"
            if failure.code not in ("budget_exhausted", "dataset_not_ready"):
                next_actions = [self.action(target, "Correct the prerequisite or arguments before retrying.")]
        except Exception:
            # Provider/database exceptions are not model-visible strings.
            data = None
            error = {"code": "internal_error", "message": "Application operation failed; consult the host without changing the experiment identity.", "retryable": False}
        result = {"ok": error is None, "tool_schema_version": self.catalog["schema_version"],
                  "campaign_id": self.service.campaign_id, "data": data,
                  "error": error, "next_actions": next_actions}
        if name in self.definitions:
            Draft202012Validator(self.definitions[name]["outputSchema"]).validate(result)
        return result
