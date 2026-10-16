"""Six research use cases plus an application-owned serial worker."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import uuid

from quant_rsi.domain.research import AttemptStatus, Candidate, Evidence, Proposal, ResearchError, check
from quant_rsi.domain.shared import DatasetSplit
from .recording import wire
from .research_ports import ExperimentExecutor, ResearchRepository
from .storage_ports import ArtifactStore


class ResearchService:
    def __init__(self, campaign_id: str, repository: ResearchRepository,
                 artifacts: ArtifactStore, executor: ExperimentExecutor):
        self.campaign_id, self.repository = campaign_id, repository
        self.artifacts, self.executor = artifacts, executor

    def execute(self, name, arguments):
        with self.repository.transaction(self.campaign_id) as campaign:
            return getattr(self, name)(campaign, **arguments)

    def search_evidence(self, campaign, query, kind, offset, limit):
        matches = []
        for evidence in sorted(campaign.evidence.values(), key=lambda e: e.evidence_id):
            if evidence.split == DatasetSplit.TEST or (kind != "all" and evidence.kind != kind):
                continue
            # Search the same permitted representation as read; never index hidden raw content.
            view = evidence.view()
            text = " ".join(str(view[k]) for k in ("title", "summary", "content")).casefold()
            if query.casefold() in text:
                matches.append({k: view[k] for k in ("evidence_id", "kind", "title", "source", "summary", "split", "access")})
        return {"items": matches[offset:offset + limit], "next_offset": offset + limit if offset + limit < len(matches) else None,
                "context": {"execution_mode": campaign.execution_mode,
                            "supported_candidate_kinds": list(campaign.supported_kinds),
                            "cases": [wire(b.case) | {"dataset_ready": b.ready} for b in campaign.cases.values()
                                      if b.case.split != DatasetSplit.TEST],
                            "candidates": [asdict(c) | {"artifact_evidence_id": next((e.evidence_id for e in campaign.evidence.values()
                                             if e.source == "artifact:" + c.artifact_sha256 and e.split != DatasetSplit.TEST), None)}
                                           for c in campaign.candidates.values()],
                            "budget": self._budget(campaign)}}

    def read_evidence(self, campaign, evidence_id):
        return campaign.read(evidence_id)

    def propose_experiment(self, campaign, **arguments):
        arguments["evidence_ids"] = tuple(arguments["evidence_ids"])
        arguments["case_ids"] = tuple(arguments["case_ids"])
        proposal = Proposal(proposal_id="proposal-" + uuid.uuid4().hex, **arguments)
        campaign.propose(proposal)
        return {"proposal": asdict(proposal), "required_evaluations": 2 * len(proposal.case_ids),
                "note": "Plan recorded; ablation and confirmation plans are not yet executed."}

    def submit_candidate(self, campaign, proposal_id, content, change_summary):
        check(proposal_id in campaign.proposals, "unknown_proposal", "Propose an experiment before submitting a candidate.")
        proposal = campaign.proposals[proposal_id]
        digest = self.artifacts.put(content.encode("utf-8"))
        # Stable identity makes an identical submission retry idempotent.
        candidate_id = "candidate-" + uuid.uuid5(uuid.NAMESPACE_URL, proposal_id + ":" + digest + ":" + change_summary).hex
        candidate = Candidate(candidate_id, proposal_id, proposal.parent_candidate_id,
                              proposal.candidate_kind, digest, change_summary)
        campaign.submit(candidate)
        evidence_id = "artifact-" + candidate_id
        campaign.evidence[evidence_id] = Evidence(evidence_id, "candidate", "Candidate " + candidate_id,
                                                 "artifact:" + digest, "Unmeasured candidate artifact. " + change_summary, content)
        return {"candidate": asdict(candidate), "artifact_stored": True, "artifact_evidence_id": evidence_id,
                "note": "Artifact registered only; no code, training or evaluation has run."}

    def request_experiment(self, campaign, proposal_id, candidate_id, idempotency_key):
        # A committed retry must not depend on the artifact backend still being available.
        if not any(a.idempotency_key == idempotency_key for a in campaign.attempts.values()):
            check(candidate_id in campaign.candidates, "unknown_candidate", "Submit a candidate first.")
            self.artifacts.get(campaign.candidates[candidate_id].artifact_sha256)
        attempt = campaign.request(proposal_id, candidate_id, idempotency_key, "attempt-" + uuid.uuid4().hex)
        return {"attempt": wire(attempt), "budget": self._budget(campaign),
                "note": "Accepted means queued, not completed or promoted. Poll get_experiment."}

    def get_experiment(self, campaign, attempt_id):
        check(attempt_id in campaign.attempts, "not_found", "Experiment not found in this campaign.")
        evidence_id = "result-" + attempt_id
        return {"attempt": wire(campaign.attempts[attempt_id]), "execution_mode": campaign.execution_mode,
                "budget": self._budget(campaign), "promotion": "not_implemented",
                "result_evidence_id": evidence_id if evidence_id in campaign.evidence else None}

    @staticmethod
    def _budget(campaign):
        return {"max_attempts": campaign.max_attempts, "accepted_attempts": len(campaign.attempts),
                "max_evaluations": campaign.max_evaluations, "reserved_evaluations": campaign.reserved_evaluations}

    def run_next(self):
        with self.repository.transaction(self.campaign_id) as campaign:
            attempt = campaign.claim()
            if attempt is None:
                return False
            attempt_id = attempt.attempt_id
            snapshot, claimed = deepcopy(campaign), deepcopy(attempt)
        # External execution is outside the repository transaction/lock.
        try:
            result = self.executor.execute(snapshot, claimed)
            expected = snapshot.proposals[claimed.proposal_id].case_ids
            check(type(result) is dict and type(result.get("comparisons")) is list,
                  "invalid_executor_result", "Executor must return paired comparisons.")
            comparisons = result["comparisons"]
            check(all(type(p) is dict and type(p.get("control")) is dict and type(p.get("candidate")) is dict for p in comparisons)
                  and tuple(p.get("case_id") for p in comparisons) == expected,
                  "invalid_executor_result", "Executor must return one paired result per bound case.")
            # Force serialization here: a malformed backend result is a failed attempt.
            import json
            json.dumps(result, allow_nan=False)
        except Exception as failure:
            safe_codes = {"invalid_demo_artifact", "model_revision_mismatch", "dataset_not_ready", "unsupported_candidate_kind", "invalid_executor_result"}
            code = failure.code if isinstance(failure, ResearchError) and failure.code in safe_codes else "execution_failed"
            with self.repository.transaction(self.campaign_id) as campaign:
                attempt = campaign.attempts[attempt_id]
                attempt.status, attempt.error_code = AttemptStatus.FAILED, code
                attempt.finished_at = datetime.now(timezone.utc).isoformat()
                # Do not return provider exception text: it can contain credentials or URLs.
                attempt.result = None
                self._record_result(campaign, attempt)
            return True
        with self.repository.transaction(self.campaign_id) as campaign:
            attempt = campaign.attempts[attempt_id]
            attempt.result, attempt.status = result, AttemptStatus.COMPLETED
            attempt.finished_at = datetime.now(timezone.utc).isoformat()
            self._record_result(campaign, attempt)
        return True

    @staticmethod
    def _record_result(campaign, attempt):
        import json
        proposal = campaign.proposals[attempt.proposal_id]
        split = (DatasetSplit.VALIDATION if any(campaign.cases[k].case.split == DatasetSplit.VALIDATION
                                              for k in proposal.case_ids) else DatasetSplit.TRAIN)
        value = {"attempt_id": attempt.attempt_id, "status": attempt.status.value,
                 "execution_mode": campaign.execution_mode, "error_code": attempt.error_code,
                 "comparisons": (attempt.result or {}).get("comparisons", []),
                 "interpretation": "Execution outcome only; mechanism and generalization remain unconfirmed."}
        content = json.dumps(value, allow_nan=False)
        evidence_id = "result-" + attempt.attempt_id
        campaign.evidence[evidence_id] = Evidence(evidence_id, "experiment", "Experiment " + attempt.attempt_id,
                                                 "experiment:" + attempt.attempt_id, content, content, split)
