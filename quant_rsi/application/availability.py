"""Explicit publication evidence; acceptance timestamps alone never expose data."""
from dataclasses import dataclass
from datetime import date, datetime
import re
from typing import Callable
from zoneinfo import ZoneInfo

from quant_rsi.domain.shared import aware, require


@dataclass(frozen=True)
class Availability:
    at: datetime
    basis: str
    estimated: bool
    evidence_sha256: str


def resolve_availability(evidence: dict, next_open: Callable[[date], datetime]) -> Availability:
    """next_open returns the first exchange opening strictly AFTER the given date.

    The filing-date policy is a declared conservative proxy, not a claim to have
    verified the historical publication instant. Its assigned date needs evidence;
    it is never derived from an acceptance timestamp or archive capture date.
    """
    digest = evidence.get('evidence_sha256', '')
    require(isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest),
            'availability evidence hash required')
    policy = evidence.get('policy')
    if policy == 'verified_release.v1':
        require(set(evidence) == {'policy', 'evidence_sha256', 'published_at'},
                'verified release requires only explicit publication evidence')
        at = datetime.fromisoformat(evidence['published_at'])
        aware(at)
        return Availability(at, policy, False, digest)
    require(policy in ('publication_date_next_open.v1', 'filing_date_next_open.v1'),
            'unsupported availability policy; acceptance alone is insufficient')
    field = 'publication_date' if policy.startswith('publication_') else 'filing_date'
    require(set(evidence) <= {'policy', 'evidence_sha256', field, 'accepted_at'}
            and field in evidence, 'explicit evidenced date required')
    day = date.fromisoformat(evidence[field])
    at = next_open(day)
    aware(at)
    require(at.astimezone(ZoneInfo('America/New_York')).date() > day,
            'conservative availability must follow the evidenced date')
    if evidence.get('accepted_at'):
        accepted = datetime.fromisoformat(evidence['accepted_at'])
        aware(accepted)
        require(accepted <= at, 'acceptance conflicts with proposed safe bound; review evidence')
    return Availability(at, policy, True, digest)
