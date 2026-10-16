from contextlib import AbstractContextManager
from typing import Protocol

from quant_rsi.domain.research import Attempt, ResearchCampaign


class ResearchRepository(Protocol):
    def create(self, campaign: ResearchCampaign) -> None: ...
    def transaction(self, campaign_id: str) -> AbstractContextManager[ResearchCampaign]:
        """Commit all workflow/budget changes atomically; rollback on exceptions."""
        ...


class ExperimentExecutor(Protocol):
    def execute(self, campaign: ResearchCampaign, attempt: Attempt) -> dict:
        """Execute paired evaluations; return sanitized JSON diagnostics, never secrets."""
        ...
