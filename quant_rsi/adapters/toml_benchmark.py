from datetime import datetime
from decimal import Decimal
import hashlib
from pathlib import Path
import tomllib

from scripts.validate_benchmark import TASK, check_fields, validate_environment, validate_task
from quant_rsi.domain.benchmark import BenchmarkTask, DatasetSplit, ExecutionRules, ProfitGoal


def load_definition(path: Path, *, require_ready: bool = False) -> BenchmarkTask:
    """Compile a configuration to a domain definition, not a data readiness proof.

    Production execution must separately verify data contents and calendar.
    Draft definitions are useful for model design and local synthetic tests.
    """
    validate_task(path, require_ready=require_ready)
    task_bytes = path.read_bytes()
    config = tomllib.loads(task_bytes.decode())
    check_fields(config, TASK, "task")
    env_bytes = (path.parent / config["environment"]).read_bytes()
    env = tomllib.loads(env_bytes.decode())
    validate_environment(env)
    exact = tomllib.loads(task_bytes.decode(), parse_float=Decimal)
    exact_env = tomllib.loads(env_bytes.decode(), parse_float=Decimal)
    episode = exact["episode"]
    goal = exact["goal"]
    costs = exact_env["execution"]
    # Length framing makes the two byte streams unambiguous.
    digest = hashlib.sha256(len(task_bytes).to_bytes(8, "big") + task_bytes + env_bytes).hexdigest()
    return BenchmarkTask(
        config["id"], config["instrument"]["symbol"], env["market"]["currency"],
        datetime.fromisoformat(episode["as_of"]), episode["horizon_sessions"],
        Decimal(episode["initial_cash"]), DatasetSplit(config["split"]),
        ProfitGoal(Decimal(goal["net_return_gte"]), Decimal(goal["max_drawdown_lte"])),
        ExecutionRules(Decimal(costs["commission_bps"]), Decimal(costs["slippage_bps"])),
        digest,
    )
