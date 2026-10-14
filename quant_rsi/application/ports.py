from datetime import datetime
from typing import Protocol

from quant_rsi.domain.benchmark import BenchmarkTask
from quant_rsi.domain.market import MarketSnapshot, TradingSession
from quant_rsi.domain.trading import Account, TargetWeight, HoldPosition, SessionSettled, TradingEpisode
from quant_rsi.domain.training import PostTrainingPlan
from .storage_ports import TrajectoryRepository  # compatibility re-export


class MarketHistory(Protocol):
    def snapshot(self, symbol: str, decision_time: datetime) -> MarketSnapshot: ...


class SessionCalendar(Protocol):
    def next_sessions(self, task: BenchmarkTask) -> tuple[TradingSession, ...]: ...


class TraderPolicy(Protocol):
    def decide(self, snapshot: MarketSnapshot, account: Account) -> TargetWeight | HoldPosition: ...


class ReplayObserver(Protocol):
    def settled(self, snapshot: MarketSnapshot, before: Account,
                target: TargetWeight | HoldPosition, event: SessionSettled) -> None: ...


class EpisodeRepository(Protocol):
    def save(self, episode: TradingEpisode) -> None: ...


class PostTrainingBackend(Protocol):
    def train(self, plan: PostTrainingPlan) -> str:
        """Return checkpoint reference; Tinker is one future adapter."""
        ...
