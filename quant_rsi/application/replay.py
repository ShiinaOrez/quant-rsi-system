from quant_rsi.domain.benchmark import BenchmarkTask
from quant_rsi.domain.evaluation import EvaluationReport, evaluate
from quant_rsi.domain.shared import require
from quant_rsi.domain.trading import TradingEpisode

from .ports import EpisodeRepository, MarketHistory, SessionCalendar, TraderPolicy, ReplayObserver


def replay(task: BenchmarkTask, episode_id: str, policy: TraderPolicy,
           history: MarketHistory, calendar: SessionCalendar,
           repository: EpisodeRepository | None = None,
           observer: ReplayObserver | None = None) -> EvaluationReport:
    """Policy receives only a snapshot and account, never future session bars."""
    sessions = calendar.next_sessions(task)
    require(len(sessions) == task.horizon_sessions, "calendar must supply exact horizon")
    episode = TradingEpisode(episode_id, task)
    for session in sessions:
        snapshot = history.snapshot(task.symbol, episode.decision_time)
        require(snapshot.symbol == task.symbol
                and snapshot.decision_time == episode.decision_time,
                "market history returned a snapshot for the wrong cutoff")
        before = episode.account
        target = policy.decide(snapshot, before)
        event = episode.settle_next_session(target, session)
        if observer is not None:
            observer.settled(snapshot, before, target, event)
        if repository is not None:
            repository.save(episode)
    return evaluate(episode)
