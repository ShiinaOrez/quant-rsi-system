from dataclasses import dataclass
from decimal import Decimal

from .shared import number, require


@dataclass(frozen=True)
class DividendEx:
    action_id: str
    amount_per_share: Decimal

    def __post_init__(self):
        require(bool(self.action_id), "dividend identity required")
        number(self.amount_per_share)


@dataclass(frozen=True)
class DividendPay:
    action_id: str

    def __post_init__(self):
        require(bool(self.action_id), "dividend identity required")
