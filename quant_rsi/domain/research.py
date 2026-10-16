"""Research workflow rules, independent of Harness, SQL and benchmark internals."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum

from .experiments import BenchmarkCase
from .shared import DatasetSplit, DomainError


class ResearchError(DomainError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def check(condition, code, message):
    if not condition:
        raise ResearchError(code, message)


class AttemptStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    kind: str
    title: str
    source: str
    summary: str
    content: str
    split: DatasetSplit | None = None

    def view(self):
        check(self.split != DatasetSplit.TEST, "not_found", "Evidence not found in the permitted view.")
        return {"evidence_id": self.evidence_id, "kind": self.kind, "title": self.title,
                "source": self.source, "summary": self.summary,
                "split": self.split.value if self.split else None,
                "content": self.summary if self.split == DatasetSplit.VALIDATION else self.content,
                "access": "summary_only" if self.split == DatasetSplit.VALIDATION else "full"}


@dataclass(frozen=True)
class ResearchCase:
    case: BenchmarkCase
    ready: bool


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    evidence_ids: tuple[str, ...]
    hypothesis: str
    falsification: str
    parent_candidate_id: str
    control_candidate_id: str
    candidate_kind: str
    change_scope: str
    case_ids: tuple[str, ...]
    ablation_plan: str
    confirmation_plan: str
    success_criteria: str


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    proposal_id: str | None
    parent_candidate_id: str | None
    kind: str
    artifact_sha256: str
    change_summary: str


@dataclass
class Attempt:
    attempt_id: str
    proposal_id: str
    candidate_id: str
    idempotency_key: str
    reserved_evaluations: int
    status: AttemptStatus = AttemptStatus.QUEUED
    result: dict | None = None
    error_code: str | None = None
    requested_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: str | None = None
    finished_at: str | None = None


@dataclass
class ResearchCampaign:
    campaign_id: str
    execution_mode: str
    max_attempts: int
    max_evaluations: int
    evidence: dict[str, Evidence]
    cases: dict[str, ResearchCase]
    candidates: dict[str, Candidate]
    supported_kinds: tuple[str, ...]
    allow_pilot: bool = False
    inspected_ids: set[str] = field(default_factory=set)
    proposals: dict[str, Proposal] = field(default_factory=dict)
    attempts: dict[str, Attempt] = field(default_factory=dict)

    def __post_init__(self):
        check(bool(self.campaign_id) and self.max_attempts > 0 and self.max_evaluations > 0,
              "invalid_campaign", "Campaign identity and positive budgets required.")

    @property
    def reserved_evaluations(self):
        return sum(a.reserved_evaluations for a in self.attempts.values())

    def read(self, evidence_id):
        check(evidence_id in self.evidence, "not_found", "Evidence not found in the permitted view.")
        view = self.evidence[evidence_id].view()
        self.inspected_ids.add(evidence_id)
        return view

    def propose(self, proposal: Proposal):
        check(proposal.proposal_id not in self.proposals, "conflict", "Proposal ID already exists.")
        check(bool(proposal.evidence_ids) and set(proposal.evidence_ids) <= self.inspected_ids,
              "evidence_not_read", "Read each cited evidence item before proposing an experiment.")
        check(proposal.parent_candidate_id in self.candidates and proposal.control_candidate_id in self.candidates,
              "unknown_candidate", "Use registered parent and control candidate IDs from discovery.")
        check(proposal.candidate_kind in self.supported_kinds, "unsupported_candidate_kind",
              "This campaign has no executor for that candidate kind.")
        check(bool(proposal.case_ids) and len(set(proposal.case_ids)) == len(proposal.case_ids),
              "invalid_cases", "At least one distinct registered case is required.")
        for case_id in proposal.case_ids:
            check(case_id in self.cases, "invalid_cases", "Case is not available to this campaign.")
            binding = self.cases[case_id]
            check(binding.case.split != DatasetSplit.TEST, "invalid_cases", "Case is not available to this campaign.")
            check(binding.ready or self.allow_pilot, "dataset_not_ready",
                  "Dataset is not ready; only a host-configured pilot campaign may execute it.")
        self.proposals[proposal.proposal_id] = proposal

    def submit(self, candidate: Candidate):
        check(candidate.proposal_id in self.proposals, "unknown_proposal", "Propose an experiment before submitting a candidate.")
        proposal = self.proposals[candidate.proposal_id]
        check(candidate.parent_candidate_id == proposal.parent_candidate_id and candidate.kind == proposal.candidate_kind,
              "candidate_scope_mismatch", "Candidate must match its proposal's parent and component kind.")
        check(candidate.artifact_sha256 != self.candidates[proposal.control_candidate_id].artifact_sha256,
              "unchanged_candidate", "Candidate artifact must differ from the registered control.")
        check(candidate.candidate_id not in self.candidates or self.candidates[candidate.candidate_id] == candidate,
              "conflict", "Candidate ID already contains different content.")
        self.candidates[candidate.candidate_id] = candidate

    def request(self, proposal_id, candidate_id, key, attempt_id):
        for previous in self.attempts.values():
            if previous.idempotency_key == key:
                check((previous.proposal_id, previous.candidate_id) == (proposal_id, candidate_id),
                      "idempotency_conflict", "Idempotency key is already bound to different inputs.")
                return previous
        check(proposal_id in self.proposals, "unknown_proposal", "Propose an experiment first.")
        check(candidate_id in self.candidates and self.candidates[candidate_id].proposal_id == proposal_id,
              "candidate_scope_mismatch", "Submit a candidate for this exact proposal before requesting execution.")
        proposal = self.proposals[proposal_id]
        evaluations = 2 * len(proposal.case_ids)
        check(len(self.attempts) < self.max_attempts and
              self.reserved_evaluations + evaluations <= self.max_evaluations,
              "budget_exhausted", "Campaign budget cannot cover another paired control/candidate evaluation.")
        attempt = Attempt(attempt_id, proposal_id, candidate_id, key, evaluations)
        self.attempts[attempt_id] = attempt
        return attempt

    def claim(self):
        # The repository serializes this transition; running work is never auto-replayed.
        if any(a.status == AttemptStatus.RUNNING for a in self.attempts.values()):
            return None
        for attempt in sorted(self.attempts.values(), key=lambda a: (a.requested_at, a.attempt_id)):
            if attempt.status == AttemptStatus.QUEUED:
                attempt.status = AttemptStatus.RUNNING
                attempt.started_at = datetime.now(timezone.utc).isoformat()
                return attempt
        return None
