import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import json
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    import jsonschema
except ImportError:
    jsonschema = None

from quant_rsi.adapters.research_store import InMemoryResearchRepository, SQLiteResearchRepository
from quant_rsi.adapters.trajectory_store import LocalArtifactStore
from quant_rsi.application.research import ResearchService
from quant_rsi.domain.experiments import BenchmarkCase, BenchmarkIdentity, BenchmarkOutcome, Metric, MetricDirection
from quant_rsi.domain.research import Candidate, Evidence, ResearchCampaign, ResearchCase
from quant_rsi.domain.shared import DatasetSplit


def campaign(artifacts):
    case = BenchmarkCase(BenchmarkIdentity("external_reasoning_fixture", "1"), "case-a", DatasetSplit.TRAIN, "a" * 64, "b" * 64)
    baseline = Candidate("baseline", None, None, "prompt", artifacts.put(b"control"), "Control")
    return ResearchCampaign("campaign", "synthetic_demo", 2, 4, {
        "public": Evidence("public", "paper", "Method", "https://example.org/paper", "public summary", "public evidence"),
        "validation": Evidence("validation", "experiment", "Allowed diagnostic", "application:validation", "allowed summary", "hidden-validation-payload", DatasetSplit.VALIDATION),
        "sealed": Evidence("sealed", "experiment", "sealed-title", "application:sealed", "sealed-summary", "sealed-content", DatasetSplit.TEST),
    }, {case.case_id: ResearchCase(case, True)}, {"baseline": baseline}, ("prompt",))


def proposal_arguments():
    return {"evidence_ids": ["public"], "hypothesis": "A general testable mechanism.", "falsification": "Measured accuracy does not improve.",
            "parent_candidate_id": "baseline", "control_candidate_id": "baseline", "candidate_kind": "prompt",
            "change_scope": "Change only the prompt.", "case_ids": ["case-a"], "ablation_plan": "Test the component separately.",
            "confirmation_plan": "Confirm on disjoint cases.", "success_criteria": "Higher accuracy with unchanged task rules."}


class Executor:
    def __init__(self):
        self.calls = 0

    def execute(self, campaign, attempt):
        self.calls += 1
        return {"comparisons": [{"case_id": "case-a", "control": {"score": "0.5"}, "candidate": {"score": "0.6"}}]}


@unittest.skipIf(jsonschema is None, "install .[research] for tool/schema tests")
class ResearchToolsTests(unittest.TestCase):
    def setUp(self):
        from quant_rsi.tools.research import ResearchTools
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.artifacts = LocalArtifactStore(Path(self.temp.name) / "blobs")
        self.repository = SQLiteResearchRepository(Path(self.temp.name) / "research.sqlite3")
        self.repository.create(campaign(self.artifacts))
        self.executor = Executor()
        self.service = ResearchService("campaign", self.repository, self.artifacts, self.executor)
        self.tools = ResearchTools(self.service)

    def call(self, name, **arguments):
        result = self.tools.call(name, arguments)
        self.assertTrue(result["ok"], result)
        return result["data"]

    def prepare(self):
        self.call("read_evidence", evidence_id="public")
        proposal_id = self.call("propose_experiment", **proposal_arguments())["proposal"]["proposal_id"]
        candidate = self.call("submit_candidate", proposal_id=proposal_id, content="candidate prompt", change_summary="One component change.")
        return proposal_id, candidate["candidate"]["candidate_id"]

    def test_workflow_gates_then_durable_result_and_searchable_feedback(self):
        blocked = self.tools.call("propose_experiment", proposal_arguments())
        self.assertEqual(blocked["error"]["code"], "evidence_not_read")
        proposal_id, candidate_id = self.prepare()
        attempt = self.call("request_experiment", proposal_id=proposal_id, candidate_id=candidate_id, idempotency_key="run-one")["attempt"]
        self.assertEqual(attempt["status"], "queued")
        self.assertEqual(self.executor.calls, 0)
        self.assertTrue(self.service.run_next())
        self.assertFalse(self.service.run_next())
        from quant_rsi.tools.research import ResearchTools
        reopened = ResearchTools(ResearchService("campaign", SQLiteResearchRepository(self.repository.path), self.artifacts, self.executor))
        result = reopened.call("get_experiment", {"attempt_id": attempt["attempt_id"]})
        self.assertEqual(result["data"]["attempt"]["status"], "completed")
        self.assertEqual(result["data"]["promotion"], "not_implemented")
        evidence = self.call("search_evidence", query="", kind="experiment", offset=0, limit=10)
        self.assertTrue(any(e["evidence_id"] == "result-" + attempt["attempt_id"] for e in evidence["items"]))

    def test_validation_raw_text_and_sealed_evidence_never_leak(self):
        found = self.call("search_evidence", query="hidden-validation-payload", kind="all", offset=0, limit=10)
        self.assertEqual(found["items"], [])
        found = self.call("search_evidence", query="", kind="all", offset=0, limit=10)
        self.assertNotIn("sealed", json.dumps(found))
        value = self.call("read_evidence", evidence_id="validation")
        self.assertEqual(value["content"], "allowed summary")
        self.assertEqual(value["access"], "summary_only")
        for evidence_id in ("sealed", "absent"):
            result = self.tools.call("read_evidence", {"evidence_id": evidence_id})
            self.assertEqual(result["error"]["code"], "not_found")

    def test_schema_blocks_unknown_fields_wrong_types_and_tool_dispatch(self):
        for arguments in ({"query": "", "kind": "all", "offset": True, "limit": 10},
                          {"query": "", "kind": "all", "offset": 0, "limit": 10, "allow_test": True}):
            result = self.tools.call("search_evidence", arguments)
            self.assertEqual(result["error"]["code"], "invalid_arguments")
        self.assertEqual(self.tools.call("run_next", {})["error"]["code"], "unknown_tool")
        result = self.tools.call("submit_candidate", {"proposal_id": "missing", "content": "x", "change_summary": "x"})
        self.assertEqual(result["error"]["code"], "unknown_proposal")

    def test_candidate_must_belong_to_requested_proposal(self):
        first, candidate_id = self.prepare()
        second = self.call("propose_experiment", **proposal_arguments())["proposal"]["proposal_id"]
        result = self.tools.call("request_experiment", {"proposal_id": second, "candidate_id": candidate_id, "idempotency_key": "wrong-parent"})
        self.assertEqual(result["error"]["code"], "candidate_scope_mismatch")
        with self.repository.transaction("campaign") as state:
            self.assertEqual(state.reserved_evaluations, 0)

    def test_duplicate_request_and_budget_reservation_are_atomic_across_connections(self):
        proposal_id, candidate_id = self.prepare()
        with self.repository.transaction("campaign") as state:
            state.max_attempts, state.max_evaluations = 1, 2
        from quant_rsi.tools.research import ResearchTools
        def request(key):
            service = ResearchService("campaign", SQLiteResearchRepository(self.repository.path), self.artifacts, self.executor)
            return ResearchTools(service).call("request_experiment", {"proposal_id": proposal_id, "candidate_id": candidate_id, "idempotency_key": key})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(request, ["same", "same"]))
        self.assertTrue(all(r["ok"] for r in results))
        self.assertEqual(results[0]["data"]["attempt"]["attempt_id"], results[1]["data"]["attempt"]["attempt_id"])
        self.assertEqual(request("different")["error"]["code"], "budget_exhausted")
        with self.repository.transaction("campaign") as state:
            self.assertEqual(state.reserved_evaluations, 2)
        self.service.run_next()
        self.assertEqual(request("same")["data"]["attempt"]["status"], "completed")
        with patch.object(self.artifacts, "get", side_effect=OSError("temporary artifact outage")):
            self.assertEqual(request("same")["data"]["attempt"]["status"], "completed")

    def test_conflicting_idempotency_key_and_unchanged_artifact_rejected(self):
        proposal_id, candidate_id = self.prepare()
        original = self.call("submit_candidate", proposal_id=proposal_id, content="candidate prompt", change_summary="One component change.")
        self.assertEqual(original["candidate"]["candidate_id"], candidate_id)
        artifact = self.call("read_evidence", evidence_id=original["artifact_evidence_id"])
        self.assertEqual(artifact["content"], "candidate prompt")
        self.call("request_experiment", proposal_id=proposal_id, candidate_id=candidate_id, idempotency_key="stable")
        second = self.call("submit_candidate", proposal_id=proposal_id, content="second prompt", change_summary="Different candidate.")["candidate"]["candidate_id"]
        result = self.tools.call("request_experiment", {"proposal_id": proposal_id, "candidate_id": second, "idempotency_key": "stable"})
        self.assertEqual(result["error"]["code"], "idempotency_conflict")
        result = self.tools.call("submit_candidate", {"proposal_id": proposal_id, "content": "control", "change_summary": "No change."})
        self.assertEqual(result["error"]["code"], "unchanged_candidate")

    def test_failed_executor_is_not_a_zero_score_and_does_not_echo_exception(self):
        class Broken:
            def execute(self, campaign, attempt):
                raise RuntimeError("provider-secret-do-not-echo")
        self.service.executor = Broken()
        proposal_id, candidate_id = self.prepare()
        attempt = self.call("request_experiment", proposal_id=proposal_id, candidate_id=candidate_id, idempotency_key="failure")["attempt"]
        self.service.run_next()
        result = self.call("get_experiment", attempt_id=attempt["attempt_id"])
        self.assertEqual(result["attempt"]["status"], "failed")
        self.assertEqual(result["attempt"]["error_code"], "execution_failed")
        self.assertIsNone(result["attempt"]["result"])
        self.assertNotIn("provider-secret", json.dumps(result))
        self.assertEqual(result["budget"]["reserved_evaluations"], 2)

    def test_unready_and_sealed_cases_are_rejected(self):
        self.call("read_evidence", evidence_id="public")
        with self.repository.transaction("campaign") as state:
            binding = state.cases["case-a"]
            state.cases["case-a"] = replace(binding, ready=False)
        result = self.tools.call("propose_experiment", proposal_arguments())
        self.assertEqual(result["error"]["code"], "dataset_not_ready")
        with self.repository.transaction("campaign") as state:
            state.cases["case-a"] = replace(binding, case=replace(binding.case, split=DatasetSplit.TEST))
        result = self.tools.call("propose_experiment", proposal_arguments())
        self.assertEqual(result["error"]["code"], "invalid_cases")

    def test_malformed_executor_result_becomes_durable_failure(self):
        class Malformed:
            def execute(self, campaign, attempt):
                return {"comparisons": []}
        self.service.executor = Malformed()
        proposal_id, candidate_id = self.prepare()
        attempt = self.call("request_experiment", proposal_id=proposal_id, candidate_id=candidate_id, idempotency_key="malformed")["attempt"]
        self.assertTrue(self.service.run_next())
        result = self.call("get_experiment", attempt_id=attempt["attempt_id"])
        self.assertEqual(result["attempt"]["status"], "failed")
        self.assertEqual(result["attempt"]["error_code"], "invalid_executor_result")
        self.assertIsNotNone(result["result_evidence_id"])
        self.assertFalse(self.service.run_next())

    def test_running_work_is_not_claimed_twice(self):
        proposal_id, candidate_id = self.prepare()
        self.call("request_experiment", proposal_id=proposal_id, candidate_id=candidate_id, idempotency_key="claim")
        with self.repository.transaction("campaign") as state:
            self.assertIsNotNone(state.claim())
        self.assertFalse(self.service.run_next())
        self.assertEqual(self.executor.calls, 0)

    def test_repository_rollback_contract_memory_and_sqlite(self):
        for repository in (InMemoryResearchRepository(), SQLiteResearchRepository(Path(self.temp.name) / "other.sqlite3")):
            repository.create(campaign(self.artifacts))
            with self.assertRaises(RuntimeError):
                with repository.transaction("campaign") as state:
                    state.max_evaluations = 999
                    raise RuntimeError("rollback")
            with repository.transaction("campaign") as state:
                self.assertEqual(state.max_evaluations, 4)

    def test_executor_uses_generic_benchmark_port_with_paired_provenance(self):
        from quant_rsi.adapters.research_executor import EvaluationExperimentExecutor
        from quant_rsi.adapters.memory_evaluations import InMemoryEvaluationRepository
        from quant_rsi.application.experiments import EvaluateCandidate
        class NonTrading:
            identity = BenchmarkIdentity("external_reasoning_fixture", "1")
            def evaluate(self, request):
                return BenchmarkOutcome(request, (Metric("accuracy", Decimal("0.8"), "ratio", MetricDirection.MAXIMIZE),), None)
        self.service.executor = EvaluationExperimentExecutor(EvaluateCandidate((NonTrading(),), InMemoryEvaluationRepository()),
                                                             lambda candidate: candidate.candidate_id + "@" + candidate.artifact_sha256)
        # External frameworks can use opaque IDs containing path separators.
        case_id = "external/suite:case-a"
        with self.repository.transaction("campaign") as state:
            binding = state.cases.pop("case-a")
            state.cases[case_id] = replace(binding, case=replace(binding.case, case_id=case_id))
        self.call("read_evidence", evidence_id="public")
        arguments = proposal_arguments() | {"case_ids": [case_id]}
        proposal_id = self.call("propose_experiment", **arguments)["proposal"]["proposal_id"]
        candidate_id = self.call("submit_candidate", proposal_id=proposal_id, content="candidate prompt", change_summary="One change.")["candidate"]["candidate_id"]
        attempt = self.call("request_experiment", proposal_id=proposal_id, candidate_id=candidate_id, idempotency_key="external")["attempt"]
        self.service.run_next()
        result = self.call("get_experiment", attempt_id=attempt["attempt_id"])["attempt"]["result"]
        pair = result["comparisons"][0]
        self.assertEqual(pair["control"]["metrics"][0]["name"], "accuracy")
        self.assertNotEqual(pair["control"]["request"]["policy_revision"], pair["candidate"]["request"]["policy_revision"])
        self.assertEqual(pair["control"]["request"]["case"], pair["candidate"]["request"]["case"])


@unittest.skipIf(jsonschema is None or importlib.util.find_spec("mcp") is None, "install .[research] for MCP integration")
class MCPIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_six_tools_through_official_stdio_client(self):
        from scripts.demo_research_tools import demonstrate
        with tempfile.TemporaryDirectory() as directory:
            result = await asyncio.wait_for(demonstrate(Path(directory)), timeout=20)
            self.assertEqual(result["tools_discovered"], 6)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["execution_mode"], "synthetic_demo")
            self.assertEqual(result["budget"]["reserved_evaluations"], 2)
            self.assertEqual(result["promotion"], "not_implemented")
            pair = result["comparisons"][0]
            self.assertEqual(pair["control"]["metrics"][0]["value"], "0")
            self.assertGreater(Decimal(pair["candidate"]["metrics"][0]["value"]), 0)
