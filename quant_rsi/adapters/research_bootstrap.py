"""Trusted composition roots; neither configuration nor file paths are tool arguments."""
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import tomllib
from urllib.request import urlopen

from quant_rsi.application.experiments import EvaluateCandidate
from quant_rsi.application.recording import wire
from quant_rsi.application.research import ResearchService
from quant_rsi.domain.experiments import BenchmarkCase
from quant_rsi.domain.market import TradingSession
from quant_rsi.domain.research import Candidate, Evidence, ResearchCampaign, ResearchCase, ResearchError, check
from quant_rsi.domain.shared import DatasetSplit
from quant_rsi.domain.trading import TargetWeight
from .frozen_pilot import FrozenPilotMarket
from .in_memory import ConstantWeightPolicy, ExplicitSessions, InMemoryHistory
from .memory_evaluations import InMemoryEvaluationRepository
from .ollama_trader import OllamaTrader, SYSTEM
from .research_executor import EvaluationExperimentExecutor
from .research_store import SQLiteResearchRepository
from .toml_benchmark import load_definition
from .trading_benchmark import TradingReplayBenchmark, ReplayCaseBinding
from .trajectory_store import LocalArtifactStore


def compose(root: Path, campaign_id="research-demo", mode="demo", *, max_attempts=3,
            max_evaluations=6, manifest=Path("artifacts/data/intc-2024-pilot/manifest.json"),
            task_path=Path("benchmarks/tasks/intc-2024-07-11-60d.toml")):
    artifacts = LocalArtifactStore(root / "blobs")
    repository = SQLiteResearchRepository(root / "research.sqlite3")
    task = load_definition(task_path)
    if mode == "demo":
        bars = tuple(TradingSession("INTC", datetime.fromisoformat(f"2024-07-{day}T09:30:00-04:00"),
                                    datetime.fromisoformat(f"2024-07-{day}T16:00:00-04:00"), Decimal(opening), Decimal(closing))
                     for day, opening, closing in (("12", "100", "105"), ("15", "105", "110")))
        config_digest = hashlib.sha256(b"synthetic-research-two-day-v1;cash=100000;fees=5bps;slippage=5bps").hexdigest()
        task = replace(task, task_id="synthetic-research-two-day", split=DatasetSplit.TRAIN,
                       horizon_sessions=2, initial_cash=Decimal(100000), configuration_digest=config_digest)
        history, calendar = InMemoryHistory(()), ExplicitSessions(bars)
        data_digest = hashlib.sha256(json.dumps(wire(bars), sort_keys=True).encode()).hexdigest()
        content, execution_mode, ready = "demo:weight=0", "synthetic_demo", True
        explanation = "Synthetic two-day prices, not historical performance. Demo prompt artifacts use demo:weight=0 through demo:weight=1."
    else:
        check(mode == "local-pilot", "invalid_mode", "Unknown application execution mode.")
        market = FrozenPilotMarket(task, manifest)
        history = calendar = market
        data_digest = market.data_digest
        content, execution_mode, ready = SYSTEM, "diagnostic_pilot", False
        explanation = "Incomplete INTC historical pilot, validation only; not a training dataset. " + "; ".join(market.manifest["limitations"])
        runtime = tomllib.loads(Path("configs/models/qwen3-8b-local.toml").read_text())
    baseline = Candidate("baseline", None, None, "prompt", artifacts.put(content.encode()), "Unchanged host-bound control")
    case = BenchmarkCase(TradingReplayBenchmark.identity, task.task_id, task.split, task.configuration_digest, data_digest)
    evidence = {
        "task-card": Evidence("task-card", "benchmark", "Bound case and interpretation", "application:case-binding", explanation, explanation, task.split),
        "baseline-prompt": Evidence("baseline-prompt", "lesson", "Unchanged baseline prompt", "artifact:" + baseline.artifact_sha256,
                                    "Read the baseline before proposing a change.", content),
    }
    initial = ResearchCampaign(campaign_id, execution_mode, max_attempts, max_evaluations,
                               evidence, {case.case_id: ResearchCase(case, ready)}, {"baseline": baseline},
                               ("prompt",), allow_pilot=mode == "local-pilot")
    try:
        with repository.transaction(campaign_id) as existing:
            check(existing.execution_mode == initial.execution_mode and existing.cases == initial.cases and
                  existing.candidates["baseline"] == baseline and existing.max_attempts == max_attempts and
                  existing.max_evaluations == max_evaluations,
                  "campaign_config_changed", "Existing campaign differs; use its original bindings or a new campaign ID and root.")
    except ResearchError as error:
        if error.code != "not_found":
            raise
        repository.create(initial)
    policies = {}

    def prepare_policy(candidate):
        check(candidate.kind == "prompt", "unsupported_candidate_kind", "Only prompt evaluation is bound.")
        prompt = artifacts.get(candidate.artifact_sha256).decode()
        revision = candidate.candidate_id + "@" + candidate.artifact_sha256
        if mode == "demo":
            check(re.fullmatch(r"demo:weight=(?:0(?:\.\d+)?|1(?:\.0+)?)", prompt) is not None,
                  "invalid_demo_artifact", "Demo artifacts must use demo:weight=<number in [0,1]>.")
            weight = TargetWeight(Decimal(prompt.split("=", 1)[1]))
            policies[revision] = lambda: ConstantWeightPolicy(weight)
        else:
            # Check the installed model before accepting the policy binding; no silent tag drift.
            with urlopen(runtime["endpoint"] + "/api/tags", timeout=30) as response:
                tags = json.load(response)
            check(any(m["name"] == runtime["model_id"] and m["digest"] == runtime["model_digest"] for m in tags["models"]),
                  "model_revision_mismatch", "Installed local model does not match the frozen runtime record.")
            revision += ":ollama:" + runtime["model_digest"]
            policies[revision] = lambda: OllamaTrader(runtime["model_id"], runtime["endpoint"], system_prompt=prompt, mandate={
                "initial_cash": str(task.initial_cash), "horizon_sessions": task.horizon_sessions,
                "goal": wire(task.goal), "execution": wire(task.execution)})
        return revision

    runner = TradingReplayBenchmark((ReplayCaseBinding(case, task, history, calendar),),
                                    lambda revision: policies[revision]())
    evaluator = EvaluateCandidate((runner,), InMemoryEvaluationRepository())
    return ResearchService(campaign_id, repository, artifacts, EvaluationExperimentExecutor(evaluator, prepare_policy))
