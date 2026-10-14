from datetime import datetime
from decimal import Decimal


class DomainError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise DomainError(message)


def aware(value: datetime) -> None:
    require(value.tzinfo is not None and value.utcoffset() is not None,
            "timestamp must be timezone aware")


def number(value: Decimal, *, minimum: Decimal = Decimal(0)) -> None:
    require(isinstance(value, Decimal) and value.is_finite() and value >= minimum,
            "expected finite Decimal within allowed range")
