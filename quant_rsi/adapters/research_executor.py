"""Connect research experiments to the existing generic evaluation use case."""
from quant_rsi.application.recording import wire
from quant_rsi.domain.experiments import EvaluationRequest
from quant_rsi.domain.research import check


class EvaluationExperimentExecutor:
    def __init__(self, evaluator, prepare_policy):
        self.evaluator, self.prepare_policy = evaluator, prepare_policy

    def execute(self, campaign, attempt):
        proposal = campaign.proposals[attempt.proposal_id]
        result = {"execution_mode": campaign.execution_mode, "comparisons": [],
                  "ablation_status": "planned_not_executed", "confirmation_status": "planned_not_executed",
                  "promotion": "not_implemented"}
        control = campaign.candidates[proposal.control_candidate_id]
        candidate = campaign.candidates[attempt.candidate_id]
        policies = {"control": self.prepare_policy(control), "candidate": self.prepare_policy(candidate)}
        for case_id in proposal.case_ids:
            binding = campaign.cases[case_id]
            check(binding.ready or campaign.allow_pilot, "dataset_not_ready", "Dataset is not ready.")
            pair = {"case_id": case_id, "dataset_ready": binding.ready}
            for role, revision in policies.items():
                request = EvaluationRequest(attempt.attempt_id + ":" + case_id + ":" + role,
                                            campaign.campaign_id, binding.case, revision)
                pair[role] = wire(self.evaluator.execute(request))
            result["comparisons"].append(pair)
        # Metrics retain suite/version/unit/direction; no cross-suite scalar averaging.
        return result
