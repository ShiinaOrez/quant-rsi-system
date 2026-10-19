import json
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from quant_rsi.adapters.ollama_history_trader import OllamaHistoryTrader
from quant_rsi.adapters.trajectory_store import SQLiteTrajectoryStore, LocalArtifactStore
from scripts.run_r0 import execute_case, baselines, run
from tests.test_frozen_archive import FrozenArchiveFixture


@unittest.skipUnless(importlib.util.find_spec('exchange_calendars'), 'optional market dependencies not installed')
class R0ExecutionTests(FrozenArchiveFixture, unittest.TestCase):
    def test_tools_trajectory_baselines_and_accounting_replay_without_network(self):
        result=self.build();market=self.market(result)
        h={'max_tool_calls':6,'max_message_bytes':12000,'require_history_evidence':True}
        policy=OllamaHistoryTrader(market,model='fixture',mandate={
            'horizon_sessions':self.task.horizon_sessions,'engine_contract':self.task.engine_contract},**h)
        steps=iter([
            {'tool':'search_history','arguments':{'kind':'filing','limit':1}},
            {'tool':'read_history','arguments':{'record_id':'original','limit':100}},
            {'action':{'target_weight':0,'rationale':'fixture'}},
        ]*2)
        def sample(_self, body):
            return {'done':True,'done_reason':'stop','message':{'content':json.dumps(next(steps))}}
        root=self.root/'run';repo=SQLiteTrajectoryStore(root/'traces.sqlite3');artifacts=LocalArtifactStore(root/'blobs')
        with patch.object(OllamaHistoryTrader,'_request',sample), \
             patch('urllib.request.urlopen',side_effect=AssertionError('no network in fixture/replay')):
            r=execute_case(self.task,market,policy,repo,artifacts,root/'case','fixture','fixture-revision',h,
                           baselines(self.task,market,'fixture'))
        self.assertEqual(r['status'],'completed',r)
        self.assertTrue(r['replay_verified'])
        self.assertEqual(set(r['baselines']),{'cash','buy_and_hold','sma20_60'})
        self.assertEqual(r['report']['net_return'],'0')
        self.assertEqual(repo.get(r['trajectory_id']).status,'completed')

    def test_failed_model_keeps_missing_metrics_and_failed_trace(self):
        result=self.build();market=self.market(result);h={'max_tool_calls':6,'max_message_bytes':12000,'require_history_evidence':False}
        policy=OllamaHistoryTrader(market,model='fixture',**h);root=self.root/'failure'
        repo=SQLiteTrajectoryStore(root/'traces.sqlite3');artifacts=LocalArtifactStore(root/'blobs')
        with patch.object(OllamaHistoryTrader,'_request',side_effect=ValueError('invalid fixture output')):
            r=execute_case(self.task,market,policy,repo,artifacts,root/'case','fixture','fixture-revision',h,
                           baselines(self.task,market,'fixture'))
        self.assertEqual(r['status'],'failed');self.assertIsNone(r['report'])
        self.assertEqual(r['failure_kind'],'agent_failure')
        self.assertEqual(repo.get(r['trajectory_id']).status,'failed')


class R0GateTests(unittest.TestCase):
    def test_ineligible_suite_does_not_contact_model_or_database(self):
        with tempfile.TemporaryDirectory() as d, \
             patch('scripts.run_r0.readiness',return_value={'formal_r0_eligible':False,'eligible_cases':0,'case_count':20}), \
             patch('scripts.run_r0.urlopen',side_effect=AssertionError('must not contact model')), \
             patch('scripts.run_r0.SQLiteTrajectoryStore',side_effect=AssertionError('must not create database')):
            r=run(Path('unused'),Path('unused'),Path(d)/'blocked',execute=True)
            self.assertEqual(r['status'],'blocked_before_inference')
            self.assertIsNone(r['formal_r0_score']);self.assertEqual(r['model_calls'],0)
