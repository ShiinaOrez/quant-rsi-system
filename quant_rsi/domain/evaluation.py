from dataclasses import dataclass
from decimal import Decimal

from .shared import require
from .trading import EpisodeStatus, TradingEpisode


@dataclass(frozen=True)
class EvaluationReport:
    task_id: str
    configuration_digest: str
    net_return: Decimal
    max_drawdown: Decimal
    turnover: Decimal
    trade_count: int
    stale_sessions: int
    goal_success: bool


def evaluate(episode: TradingEpisode) -> EvaluationReport:
    require(episode.status == EpisodeStatus.COMPLETED, "only completed episodes can be scored")
    task = episode.task
    peak, drawdown = task.initial_cash, Decimal(0)
    for event in episode.events:
        peak = max(peak, event.equity)
        drawdown = max(drawdown, 1 - event.equity / peak)
    net_return = episode.events[-1].equity / task.initial_cash - 1
    return EvaluationReport(
        task.task_id, task.configuration_digest, net_return, drawdown,
        sum((e.turnover for e in episode.events), Decimal(0)),
        sum(e.shares_delta != 0 for e in episode.events),
        sum(e.stale_valuation for e in episode.events),
        net_return >= task.goal.net_return_gte and drawdown <= task.goal.max_drawdown_lte,
    )
