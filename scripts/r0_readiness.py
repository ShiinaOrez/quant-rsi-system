"""Per-case R0 gates. No training keys, database or inference are required.

Acquired source material is inventory, not proof of a complete/PIT-ready filing.
Known reference accessions without a compiled coverage binding remain explicit.
"""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import tomllib

from quant_rsi.adapters.frozen_archive import FrozenArchiveMarket, sha256
from quant_rsi.adapters.toml_benchmark import load_definition
from quant_rsi.domain.shared import require
from scripts.archive_bounds import suite_bounds
from scripts.validate_suite import validate_suite

FORMS = {'10-K', '10-K/A', '10-Q', '10-Q/A', '8-K', '8-K/A'}


def inventory(config, root):
    result = defaultdict(list)
    inputs = {}
    def read(p):
        p = root / p
        data = p.read_bytes()
        inputs[str(p.relative_to(root))] = sha256(data)
        return data
    def add(acc, scope, data, digest, origin):
        require(sha256(data) == digest, 'source inventory integrity failure: ' + origin)
        result[acc].append({'scope': scope, 'sha256': digest, 'bytes': len(data), 'origin': origin})
    for entry in config.get('single_documents', []):
        add(entry['accession'],entry['scope'],read(entry['path']),entry['sha256'],entry['path'])
    for folder in config.get('primary_exports', []):
        for p in sorted((root / folder).glob('*.content.provenance.json')):
            r = json.loads(read(p.relative_to(root)))
            # *.content.provenance.json -> *.content
            body = Path(str(p).removesuffix('.provenance.json'))
            add(r['reference']['accession'], 'primary_document', body.read_bytes(),
                r['content']['sha256'], str(body.relative_to(root)))
    for spec in config.get('q4', []):
        report = json.loads(read(spec['summary']))
        for e in report['entries']:
            acc = e.get('identity', {}).get('accession_candidate')
            if e.get('document_type') != 'XBRL' or not acc:
                continue
            digest = e['sha256']
            body = root / spec['store'] / 'blobs' / digest[:2] / digest
            checks = e.get('corroboration', {}).get('checks', {})
            numerical = e.get('parse', {}).get('counts', {}).get('numericItem', {}).get('total', 0)
            scope = ('financial_instance_candidate' if numerical else 'cover_or_unreviewed_xbrl')
            if any(value is False for value in checks.values()):
                scope = 'identity_or_period_conflict'
            add(acc, scope, body.read_bytes(), digest, str(body.relative_to(root)))
    for name in config.get('quarterly_audits', []):
        report = json.loads(read(name))
        for e in report['documents']:
            add(e['accession'], 'financial_instance_candidate', (root / e['instance_file']).read_bytes(),
                e['sha256'], e['instance_file'])
    for name in config.get('mirror_inventories', []):
        report = json.loads(read(name))
        for e in report['filings']:
            add(e['accession'], 'issuer_xbrl_zip_unbound', (root / e['path']).read_bytes(), e['sha256'], e['path'])
    for acc, rows in result.items():
        seen = set()
        result[acc] = [r for r in rows if (r['scope'], r['sha256']) not in seen
                       and not seen.add((r['scope'], r['sha256']))]
    return result, inputs


def readiness(config_path):
    config_path = Path(config_path).resolve()
    config = tomllib.loads(config_path.read_text())
    require(config['schema_version'] == 'r0_readiness_inputs.v1', 'unsupported R0 inputs')
    root = (config_path.parent / config['repository_root']).resolve()
    suite = root / config['suite']
    validate_suite(suite, with_calendar=True)
    suite_data = tomllib.loads(suite.read_text())
    paths = [(suite.parent / t).resolve() for t in suite_data['legacy_tasks']]
    paths += [(suite.parent / c['task']).resolve() for c in suite_data['cases']]
    windows = {r['task_id']: r for r in suite_bounds(suite)['tasks']}
    acquired, inputs = inventory(config.get('inventory', {}), root)
    inputs[str(config_path.relative_to(root))] = sha256(config_path.read_bytes())
    reference = {}
    for entry in config['references']:
        p = root / entry['path']
        data = p.read_bytes()
        require(sha256(data) == entry['sha256'], 'reference index integrity failure')
        rows = [json.loads(l) for l in data.decode().splitlines()]
        require(all(int(r['cik']) == entry['cik'] for r in rows), 'reference CIK mismatch')
        reference[entry['symbol']] = [r for r in rows if r['form'] in FORMS]
        inputs[entry['path']] = entry['sha256']
    market_path = root / config['market_audit']['path']
    market_data = market_path.read_bytes()
    require(sha256(market_data) == config['market_audit']['sha256'], 'market audit integrity failure')
    inputs[config['market_audit']['path']] = sha256(market_data)
    market = {r['task_id']: r for r in json.loads(market_data)['tasks']}
    cases = []
    for path in paths:
        raw = path.read_bytes()
        task_config = tomllib.loads(raw.decode())
        task = load_definition(path)
        inputs[str(path.relative_to(root))] = sha256(raw)
        through = windows[task.task_id]['terminal_session']
        cutoff = task.as_of.date().isoformat()
        refs = [r for r in reference[task.symbol] if r['filingDate'] <= through]
        before = [r for r in refs if r['filingDate'] <= cutoff]
        latest = {}
        for form in ('10-K', '10-Q'):
            matches = [r for r in before if r['form'] == form]
            r = max(matches, key=lambda r: (r['filingDate'], r['accessionNumber'])) if matches else None
            latest[form] = ({'accession': r['accessionNumber'], 'filing_date': r['filingDate'],
                             'report_date': r.get('reportDate'),
                             'acquired_scopes': sorted({e['scope'] for e in acquired[r['accessionNumber']]})}
                            if r else None)
        audit = market.get(task.task_id, {})
        market_pass = (audit.get('price_window_coverage_pass') is True
                       and audit.get('task_file_sha256') == sha256(raw))
        blockers = []
        compiled = None
        dataset = task_config['dataset']
        if dataset['manifest'] == 'UNBOUND' or dataset['sha256'] == 'UNBOUND':
            blockers.append({'code': 'dataset_unbound', 'category': 'implementation_binding',
                             'detail': 'No task-bound frozen archive; downloaded source files do not satisfy this gate'})
            for code in sorted(('filings_history', 'filing_exhibits', 'financial_statements',
                                'market_history_and_actions', 'permitted_use')):
                blockers.append({'code': code, 'category': 'data_admission',
                                 'detail': 'No hash-bound reviewed admission evidence for this task'})
        else:
            try:
                compiled = FrozenArchiveMarket(task, path.parent / dataset['manifest'],
                    expected_sha256=dataset['sha256'], require_admission=False)
                if compiled.reference_digest is not None:
                    require(compiled.reference_digest == next(e['sha256'] for e in config['references'] if e['symbol'] == task.symbol),
                            'bundle reference differs from fixed suite index')
                for code in compiled.blockers:
                    blockers.append({'code': code, 'category': 'data_admission', 'detail': 'Compiled admission unresolved'})
            except (ValueError, OSError, KeyError, TypeError) as e:
                blockers.append({'code': 'bundle_invalid', 'category': 'implementation_binding', 'detail': str(e)})
        if task_config['status'] != 'ready':
            blockers.append({'code': 'task_draft', 'category': 'review',
                             'detail': 'Explicit promotion follows data admission; compiler never promotes tasks'})
        if not market_pass:
            blockers.append({'code': 'market_window_audit', 'category': 'data_admission', 'detail': 'Window audit missing or stale'})
        rows = [{'accession': r['accessionNumber'], 'form': r['form'], 'filing_date': r['filingDate'],
                 'phase': 'initial_date_candidate' if r['filingDate'] <= cutoff else 'during_episode_date_candidate',
                 'acquired_scopes': sorted({e['scope'] for e in acquired[r['accessionNumber']]})}
                for r in refs]
        cases.append({'task_id': task.task_id, 'symbol': task.symbol, 'cutoff': task.as_of.isoformat(),
                      'terminal_session': through, 'formal_r0_eligible': not blockers,
                      'blockers': blockers, 'market_window_audited': market_pass,
                      'forward_splits': audit.get('forward_splits'),
                      'expected_six_form_filings': len(refs), 'initial_date_candidates': len(before),
                      'acquired_any_scope': sum(bool(r['acquired_scopes']) for r in rows),
                      'latest_initial_filings': latest, 'filing_inventory': rows,
                      'priority_source_checks': [dict(r, form=form, action='check existing caches, then acquire missing usable content')
                          for form,r in latest.items() if r and not ({'primary_document','financial_instance_candidate'} & set(r['acquired_scopes']))],
                      'coverage_status': 'reviewed_bundle' if compiled and not compiled.blockers else 'unbound',
                      'phase_note': 'Filing dates group inventory only; availability policy decides actual model visibility'})
    return {'schema_version': 'r0_readiness.v1', 'suite': suite_data['id'], 'case_count': len(cases),
            'eligible_cases': sum(c['formal_r0_eligible'] for c in cases),
            'formal_r0_eligible': all(c['formal_r0_eligible'] for c in cases),
            'resource_dependencies': ['fixed_local_qwen', 'local_trajectory_and_artifact_ports'],
            'not_required_for_r0': ['tinker', 'deepseek', 'supabase', 'training_dataset'],
            'source_inventory_entries': sum(len(v) for v in acquired.values()),
            'inventory_scope': 'Only explicitly configured caches, verified bytes; absence means not inventoried, not proof file is unavailable',
            'inputs': inputs, 'cases': cases}


def markdown(report):
    lines = ['# R0 逐题准入', '',
             f"{report['eligible_cases']}/{report['case_count']} 题已准入；正式 R0 {'可执行' if report['formal_r0_eligible'] else '仍阻断'}。", '',
             '计数为固定参考索引中截至各题终点的六类表单。已取得列包含正文、实例、封面或未绑定 ZIP，不能当作完整申报数。日期分组不代表已开放模型可见性。', '',
             '| 题目 | 参考申报 | 已取得某类内容 | 价格窗口 | 最新季报的已取得范围 |',
             '|---|---:|---:|---|---|']
    for c in report['cases']:
        q = c['latest_initial_filings']['10-Q']
        scopes = ', '.join(q['acquired_scopes']) or '未纳入来源清单' if q else '无参考记录'
        lines.append(f"| {c['task_id']} | {c['expected_six_form_filings']} | {c['acquired_any_scope']} | {'通过' if c['market_window_audited'] else '待查'} | {scopes} |")
    lines += ['', '阻断项按题保存在 JSON 的 `blockers`，每份参考申报及来源范围在 `filing_inventory`。当前来源清单缺失不等于磁盘没有该文件。R0 不依赖 Tinker、DeepSeek、Supabase 或训练集。', '']
    return '\n'.join(lines)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, default=Path('configs/benchmark/r0-readiness.toml'))
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    require(not a.output.exists(), 'readiness output must be a new snapshot')
    report = readiness(a.config)
    a.output.mkdir(parents=True)
    (a.output / 'readiness.json').write_text(json.dumps(report, indent=2) + '\n')
    (a.output / 'readiness.md').write_text(markdown(report))
    print(json.dumps({k: report[k] for k in ('case_count', 'eligible_cases', 'formal_r0_eligible', 'source_inventory_entries')}))
