"""Local, all-case R0 runner. Refuses incomplete admission before contacting Qwen.

Storage is supplied through trajectory/artifact ports; CLI defaults to local
SQLite/CAS. No training service or cloud database is consulted.
"""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import tomllib
from urllib.request import urlopen

from quant_rsi.adapters.frozen_archive import FrozenArchiveMarket, sha256
from quant_rsi.adapters.ollama_history_trader import OllamaHistoryTrader
from quant_rsi.adapters.toml_benchmark import load_definition
from quant_rsi.adapters.trajectory_store import SQLiteTrajectoryStore, LocalArtifactStore
from quant_rsi.adapters.in_memory import ConstantWeightPolicy
from quant_rsi.application.recording import ReplayRecorder, wire
from quant_rsi.application.replay import replay
from quant_rsi.application.trajectory_export import export_jsonl
from quant_rsi.domain.shared import require, DomainError
from quant_rsi.domain.trading import TargetWeight, HoldPosition
from quant_rsi.domain.training import TrajectoryReference
from scripts.r0_readiness import readiness


class AgentFailure(Exception):
    pass


class BuyAndHold:
    def __init__(self):
        self.first = True

    def decide(self, snapshot, account):
        action = TargetWeight(Decimal(1)) if self.first else HoldPosition()
        self.first = False
        return action


class MovingAverage:
    def decide(self, snapshot, account):
        bar = json.loads(next(r.content for r in snapshot.records if r.record_id.startswith('bar-')))
        require(bar['sma20'] is not None and bar['sma60'] is not None, 'baseline warmup missing')
        return TargetWeight(Decimal(int(bar['sma20'] > bar['sma60'])))


def baselines(task, market, run_id):
    return {name: wire(replay(task, run_id+'-'+name, policy, market, market))
            for name, policy in [('cash', ConstantWeightPolicy(TargetWeight(Decimal(0)))),
                                 ('buy_and_hold', BuyAndHold()), ('sma20_60', MovingAverage())]}


def replay_trace(task, market, trace, artifacts, original_report, history_config):
    samples = [json.loads(artifacts.get(json.loads(e.payload_json)['artifact_sha256']))
               for e in trace.events if e.event_type == 'policy_sample']
    require(bool(samples), 'model sample trace required')
    original_observation = json.loads(samples[0]['request']['messages'][1]['content'])
    tools = []
    class Recorded(OllamaHistoryTrader):
        def __init__(self):
            super().__init__(market, model=samples[0]['request']['model'],
                             seed=samples[0]['request']['options']['seed'],
                             mandate=original_observation['mandate'], tool_capture=tools.append,
                             **history_config)
            self.index = 0

        def _request(self, body):
            require(self.index < len(samples), 'missing saved model call')
            sample = samples[self.index]
            require(sample['request'] == body and sample['decision_index'] == self.decisions
                    and sample['protocol_revision'] == self.protocol_revision,
                    'saved prompt or tool evidence differs during replay')
            self.index += 1
            return sample['response']
    policy = Recorded()
    reproduced = replay(task, trace.run_id, policy, market, market)
    require(policy.index == len(samples), 'unused model calls')
    require(tools == [json.loads(e.payload_json) for e in trace.events if e.event_type == 'history_tool'],
            'tool transcript replay mismatch')
    require(wire(reproduced) == wire(original_report), 'accounting replay mismatch')


def execute_case(task, market, policy, repository, artifacts, output, run_id, policy_revision,
                 history_config, baseline_reports):
    """Ports are injected; tests and future serverless storage use the same path."""
    output.mkdir(parents=True, exist_ok=False)
    ref = TrajectoryReference(run_id+'-trader',task.task_id,task.split,task.configuration_digest,
                              market.data_digest,policy_revision)
    def progress(step, event):
        print(json.dumps({'task_id':task.task_id,'step':step,'total':task.horizon_sessions,
                          'equity':str(event.equity)}),flush=True)
    recorder = ReplayRecorder(ref,run_id,artifacts,progress)
    policy.capture = recorder.sampled
    policy.tool_capture = lambda e: recorder.add('history_tool',e)
    result = {'task_id':task.task_id,'run_id':run_id,'configuration_digest':task.configuration_digest,
              'data_digest':market.data_digest,'policy_revision':policy_revision,
              'history_policy':history_config,'baselines':baseline_reports,'report':None,
              'status':'not_executed','trajectory_id':ref.trajectory_id,'replay_verified':False}
    try:
        from jsonschema.exceptions import ValidationError
        class PolicyBoundary:
            def decide(self, snapshot, account):
                try:
                    return policy.decide(snapshot, account)
                except (DomainError, ValueError, ValidationError) as e:
                    raise AgentFailure(str(e)) from e
        report = replay(task,run_id,PolicyBoundary(),market,market,observer=recorder)
        recorder.add('evaluation',report)
        trace = recorder.finalize('completed')
        replay_trace(task,market,trace,artifacts,report,history_config)
        result.update(status='completed',report=wire(report),replay_verified=True)
    except Exception as e:
        # Preserve real failure; no synthetic zero returns. Replay failure is infrastructure.
        result.update(status='failed',failure_kind='agent_failure' if isinstance(e,AgentFailure) else 'infra_failure',
                      error_type=type(e.__cause__ or e).__name__,error=str(e))
        if 'trace' not in locals():
            recorder.add('failure',{'error_type':type(e).__name__,'message':str(e)})
            trace = recorder.finalize('failed')
    repository.save(trace)
    (output/'trajectory.jsonl').write_bytes(export_jsonl(repository,ref.trajectory_id))
    (output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


def run(config_path, runtime_path, output, *, execute=False):
    require(not output.exists(), 'run output must be new')
    preflight = readiness(config_path)
    output.mkdir(parents=True)
    (output/'preflight.json').write_text(json.dumps(preflight,indent=2)+'\n')
    if not preflight['formal_r0_eligible']:
        result = {'status':'blocked_before_inference','eligible_cases':preflight['eligible_cases'],
                  'total_cases':preflight['case_count'],'model_calls':0,'formal_r0_score':None,
                  'reason':'Per-case admission unresolved; see preflight.json'}
        (output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
        return result
    if not execute:
        return {'status':'ready_not_executed','formal_r0_score':None}
    config_path = config_path.resolve()
    config = tomllib.loads(config_path.read_text())
    root = (config_path.parent/config['repository_root']).resolve()
    suite_path = root/config['suite']
    s = tomllib.loads(suite_path.read_text())
    paths = [(suite_path.parent/t).resolve() for t in s['legacy_tasks']]
    paths += [(suite_path.parent/c['task']).resolve() for c in s['cases']]
    bindings = []
    # Validate/load ALL cases and dry-run all baseline engines before any paid/model calls.
    for path in paths:
        task = load_definition(path,require_ready=True)
        d = tomllib.loads(path.read_text())['dataset']
        market = FrozenArchiveMarket(task,path.parent/d['manifest'],expected_sha256=d['sha256'])
        bindings.append((task,market,baselines(task,market,task.task_id)))
    runtime = tomllib.loads(runtime_path.read_text())
    require(runtime['temperature'] == 0 and runtime['thinking'] is False
            and runtime['context_tokens'] == 8192 and runtime['max_output_tokens'] == 192,
            'runtime settings differ from frozen Ollama history protocol')
    history = {'max_tool_calls':6,'max_message_bytes':12000,'require_history_evidence':False}
    # Constructor enforces local-only endpoint before /api/tags is requested.
    def make_policy(task, market):
        return OllamaHistoryTrader(market,runtime['model_id'],runtime['endpoint'],seed=runtime['seed'],
            mandate={'horizon_sessions':task.horizon_sessions,'initial_cash':str(task.initial_cash),
                     'goal':wire(task.goal),'execution':wire(task.execution),'engine_contract':task.engine_contract},
            **history)
    endpoint = make_policy(bindings[0][0],bindings[0][1]).base_url
    def check_model():
        with urlopen(endpoint+'/api/tags',timeout=15) as response:
            tags = json.load(response)
        require(any(m['name']==runtime['model_id'] and m['digest']==runtime['model_digest']
                    for m in tags['models']), 'local Qwen revision mismatch')
    check_model()
    code_paths = ['quant_rsi/adapters/ollama_history_trader.py','quant_rsi/adapters/ollama_trader.py',
                  'quant_rsi/tools/history-tools.v1.json','quant_rsi/domain/trading.py',
                  'quant_rsi/adapters/frozen_archive.py','quant_rsi/application/availability.py',
                  'quant_rsi/application/history.py','quant_rsi/tools/history.py','scripts/run_r0.py']
    fingerprint = {'runtime_sha256':sha256(runtime_path.read_bytes()),'history_config':history,
                   'code_sha256':{p:sha256((root/p).read_bytes()) for p in code_paths}}
    (output/'runtime-lock.json').write_text(json.dumps(fingerprint,indent=2)+'\n')
    revision = 'ollama:'+runtime['model_id']+'@'+runtime['model_digest']+'/'+sha256(json.dumps(fingerprint,sort_keys=True).encode())
    repository = SQLiteTrajectoryStore(output/'trajectories.sqlite3')
    artifacts = LocalArtifactStore(output/'blobs')
    results = []
    for task,market,baseline in bindings:
        check_model()
        r = execute_case(task,market,make_policy(task,market),repository,artifacts,output/task.task_id,
                         output.name+'-'+task.task_id,revision,history,baseline)
        check_model()
        results.append(r)
        (output/'cases.json').write_text(json.dumps(results,indent=2)+'\n')
        print(json.dumps({'task_id':task.task_id,'status':r['status']}),flush=True)
    result = {'status':'executed','total_cases':len(results),
              'completed':sum(r['status']=='completed' for r in results),
              'failed':sum(r['status']=='failed' for r in results),
              'agent_failures':sum(r.get('failure_kind')=='agent_failure' for r in results),
              'infra_failures':sum(r.get('failure_kind')=='infra_failure' for r in results),
              'goal_success':sum(bool(r['report'] and r['report']['goal_success']) for r in results),
              'cases':results,'training_performed':False}
    if result['infra_failures']:
        result.update(status='infra_failure',formal_r0_score=None)
    (output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,default=Path('configs/benchmark/r0-readiness.toml'))
    p.add_argument('--runtime',type=Path,default=Path('configs/models/qwen3-8b-local.toml'))
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--execute',action='store_true')
    a=p.parse_args()
    existed = a.output.exists()
    try:
        result = run(a.config,a.runtime,a.output,execute=a.execute)
    except Exception as error:
        result = {'status':'infra_failure','error_type':type(error).__name__,'error':str(error),
                  'formal_r0_score':None,'model_calls':None}
        if not existed and a.output.is_dir():
            with (a.output/'failure.json').open('x') as f:
                json.dump(result,f,indent=2)
    print(json.dumps(result,indent=2))
    raise SystemExit({'blocked_before_inference':2,'infra_failure':3}.get(result['status'],0))
