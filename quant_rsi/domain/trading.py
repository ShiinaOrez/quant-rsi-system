from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
from enum import StrEnum

from .benchmark import BenchmarkTask
from .market import TradingSession
from .shared import number, require


class EpisodeStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"


@dataclass(frozen=True)
class TargetWeight:
    value: Decimal

    def __post_init__(self):
        number(self.value)
        require(self.value <= 1, "target weight must be in [0, 1]")


@dataclass(frozen=True)
class Account:
    cash: Decimal
    shares: int

    def __post_init__(self):
        number(self.cash)
        require(type(self.shares) is int and self.shares >= 0, "nonnegative integer shares required")


@dataclass(frozen=True)
class SessionSettled:
    episode_id: str
    session_close: datetime
    shares_delta: int
    fill_price: Decimal | None
    commission: Decimal
    turnover: Decimal
    account: Account
    equity: Decimal
    stale_valuation: bool
    rejection: str | None


class TradingEpisode:
    """Aggregate: one account, one clock, one append-only session history.

    Commands are serialized by the application. A failed command leaves the
    account and clock unchanged. Corporate actions await a dedicated ledger.
    """

    def __init__(self, episode_id: str, task: BenchmarkTask):
        require(bool(episode_id), "episode identity required")
        self._id, self._task = episode_id, task
        self._account = Account(task.initial_cash, 0)
        self._decision_time = task.as_of
        self._last_close = None
        self._events: list[SessionSettled] = []

    @property
    def task(self):
        return self._task

    @property
    def account(self):
        return self._account

    @property
    def decision_time(self):
        return self._decision_time

    @property
    def events(self):
        return tuple(self._events)

    @property
    def status(self):
        return (EpisodeStatus.COMPLETED if len(self._events) == self._task.horizon_sessions
                else EpisodeStatus.ACTIVE)

    def settle_next_session(self, target: TargetWeight, session: TradingSession) -> SessionSettled:
        require(self.status == EpisodeStatus.ACTIVE, "episode already completed")
        require(session.symbol == self.task.symbol, "wrong instrument")
        require(session.opens_at > self.decision_time, "session must follow decision cutoff")
        require(not session.corporate_actions, "corporate action ledger not implemented")
        close = session.raw_close if session.raw_close is not None else self._last_close
        require(close is not None, "cannot value session without a known close")
        account = self.account
        delta, fill, fee, turnover, rejection = 0, None, Decimal(0), Decimal(0), None
        if not session.tradable or session.raw_open is None:
            rejection = "untradable" if not session.tradable else "missing_open"
        else:
            price = session.raw_open
            equity = account.cash + account.shares * price
            desired = int((equity * target.value / price).to_integral_value(rounding=ROUND_FLOOR))
            delta = desired - account.shares
            slip = self.task.execution.slippage_bps / 10000
            rate = self.task.execution.commission_bps / 10000
            fill = price * (1 + slip if delta > 0 else 1 - slip)
            if delta > 0:
                affordable = int((account.cash / (fill * (1 + rate))).to_integral_value(rounding=ROUND_FLOOR))
                delta = min(delta, affordable)
            if delta:
                notional = abs(delta) * fill
                fee = notional * rate
                turnover = notional / equity
                account = Account(account.cash - delta * fill - fee, account.shares + delta)
            else:
                fill = None
        event = SessionSettled(self._id, session.closes_at, delta, fill, fee, turnover,
                               account, account.cash + account.shares * close,
                               session.raw_close is None, rejection)
        self._account = account
        self._last_close = close
        self._decision_time = session.closes_at
        self._events.append(event)
        return event
