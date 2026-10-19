"""Compile normalized records into a pinned bundle; never edits/promotes a task.

JSONL is data, TOML describes inputs. Source collectors must supply content and
explicit availability evidence; accepted_at or latest website content is not enough.
"""
import argparse
import json
from pathlib import Path
import tomllib

from quant_rsi.application.availability import resolve_availability
from quant_rsi.adapters.frozen_archive import FrozenArchiveMarket, next_exchange_open, sha256
from quant_rsi.adapters.toml_benchmark import load_definition
from quant_rsi.domain.shared import require


def compile_bundle(task, records, sessions, admission, evidence_files, output):
    """evidence_files maps safe bundle-relative paths to verified local source paths.

    Admission is a separately reviewed evidence report, not manufactured by this
    compiler. An incomplete report produces a usable diagnostic archive with the
    blockers preserved. Formal loading requires all five review checks to pass.
    """
    output = Path(output)
    require(not output.exists(), 'output must be a new immutable bundle')
    normalized = []
    for item in records:
        row = dict(item)
        row.setdefault('filing_accession', None)
        row.setdefault('originating_record_id', None)
        require('available_at' not in row and 'availability_basis' not in row,
                'input cannot supply a precomputed visibility time')
        evidence = row['availability_input']
        available = resolve_availability(evidence, next_exchange_open)
        row.update(available_at=available.at.isoformat(), availability_basis=available.basis,
                   availability_estimated=available.estimated,
                   availability_evidence_sha256=available.evidence_sha256)
        normalized.append(row)
    # A failed build is preserved in its new directory, never overwrites old data.
    output.mkdir(parents=True)
    entries = []
    def put(name, data, role):
        target = output / name
        require(not Path(name).is_absolute() and target.resolve().is_relative_to(output.resolve()),
                'bundle path escapes root')
        require(name not in {e['path'] for e in entries}, 'duplicate bundle output')
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as f:
            f.write(data)
        entries.append({'path': name, 'sha256': sha256(data), 'role': role})
    put('records.jsonl', ''.join(json.dumps(r, sort_keys=True) + '\n' for r in normalized).encode(), 'records')
    put('sessions.json', json.dumps(sessions, sort_keys=True).encode(), 'sessions')
    put('admission.json', json.dumps(admission, sort_keys=True).encode(), 'admission')
    for name, source in evidence_files.items():
        put(name, Path(source).read_bytes(), 'evidence')
    m = {'schema_version': 'frozen_archive.v1', 'task_id': task.task_id, 'symbol': task.symbol,
         'as_of': task.as_of.isoformat(), 'horizon_sessions': task.horizon_sessions,
         'engine_contract': task.engine_contract, 'files': entries}
    manifest = output / 'manifest.json'
    manifest.write_text(json.dumps(m, indent=2) + '\n')
    market = FrozenArchiveMarket(task, manifest, expected_sha256=sha256(manifest.read_bytes()),
                                 require_admission=False)
    result = {'manifest': str(manifest), 'sha256': market.data_digest,
              'compiled_records': len(normalized), 'admission_blockers': market.blockers,
              'task_promoted': False, 'reviewed_admission_passed': not market.blockers}
    (output / 'build-result.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--spec', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    spec = tomllib.loads(a.spec.read_text())
    require(spec['schema_version'] == 'frozen_archive_build.v1', 'unsupported build spec')
    base = a.spec.parent
    result = compile_bundle(load_definition(base / spec['task']),
        [json.loads(l) for l in (base / spec['records']).read_text().splitlines()],
        json.loads((base / spec['sessions']).read_text()), json.loads((base / spec['admission']).read_text()),
        {r['bundle_path']: base / r['source_path'] for r in spec['evidence']}, a.output)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
