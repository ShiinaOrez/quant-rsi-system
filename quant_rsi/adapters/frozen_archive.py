"""Hash-bound archive adapter, independent of acquisition source and storage DB.

Only allowlisted record fields cross into the history port. Sessions, coverage,
provenance and admission evidence stay in the environment, outside model tools.
"""
from datetime import datetime, timedelta
from functools import lru_cache
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from quant_rsi.application.history import HistoryItem, KINDS
from quant_rsi.application.availability import resolve_availability
from quant_rsi.domain.corporate_actions import DividendEx, DividendPay
from quant_rsi.domain.market import InformationRecord, TradingSession, snapshot_at
from quant_rsi.domain.shared import require, aware

ADMISSION_CHECKS = frozenset({'filings_history', 'filing_exhibits', 'financial_statements',
                              'market_history_and_actions', 'permitted_use'})
BAR_FIELDS = frozenset({'session', 'close', 'volume', 'sma20', 'sma60', 'rsi14', 'daily_volatility20'})


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def exchange_calendar(task):
    import exchange_calendars as xc
    return xc.get_calendar('XNAS', start=(task.as_of - timedelta(days=500)).date().isoformat(),
                           end=(task.as_of + timedelta(days=task.horizon_sessions * 3 + 30)).date().isoformat())


@lru_cache(maxsize=8192)
def next_exchange_open(day):
    import exchange_calendars as xc
    cal = xc.get_calendar('XNAS', start=day.isoformat(), end=(day + timedelta(days=30)).isoformat())
    session = next(s for s in cal.sessions if s.date() > day)
    return cal.session_open(session).to_pydatetime()


class FrozenArchiveMarket:
    def __init__(self, task, manifest_path: Path, *, expected_sha256: str, require_admission=True):
        self.task = task
        root = Path(manifest_path).parent.resolve()
        raw = Path(manifest_path).read_bytes()
        require(sha256(raw) == expected_sha256, 'manifest digest differs from task binding')
        self.data_digest = sha256(raw)
        self.manifest = m = json.loads(raw)
        require(m['schema_version'] == 'frozen_archive.v1', 'unsupported frozen archive')
        require(m['task_id'] == task.task_id and m['symbol'] == task.symbol
                and datetime.fromisoformat(m['as_of']) == task.as_of
                and m['horizon_sessions'] == task.horizon_sessions
                and m['engine_contract'] == task.engine_contract, 'bundle/task identity mismatch')
        files = {}
        roles = {}
        for entry in m['files']:
            path = (root / entry['path']).resolve()
            require(not Path(entry['path']).is_absolute() and path.is_relative_to(root), 'bundle path escapes root')
            require(entry['path'] not in files, 'duplicate bundle file')
            data = path.read_bytes()
            require(sha256(data) == entry['sha256'], 'bundle file integrity failure')
            files[entry['path']] = data
            if entry.get('role') in ('records', 'sessions', 'admission'):
                require(entry['role'] not in roles, 'duplicate bundle role')
                roles[entry['role']] = entry['path']
        require(set(roles) == {'records', 'sessions', 'admission'}, 'missing bundle role')
        admission = json.loads(files[roles['admission']])
        require(admission['schema_version'] == 'archive_admission.v1', 'unsupported admission report')
        checks = admission['checks']
        require(len({c['code'] for c in checks}) == len(checks), 'duplicate admission check')
        require({c['code'] for c in checks} == ADMISSION_CHECKS, 'admission checklist incomplete')
        self.blockers = []
        for check in checks:
            require(check['status'] in ('passed', 'blocked', 'not_reviewed'), 'invalid admission status')
            evidence = check.get('evidence_paths', [])
            require(all(p in files and p not in roles.values() for p in evidence),
                    'admission evidence not separately hash-bound')
            if check['status'] != 'passed' or not evidence:
                self.blockers.append(check['code'])
        if require_admission:
            require(not self.blockers, 'archive admission blocked: ' + ','.join(self.blockers))
        self.admission = admission
        self.reference_digest = None
        bound_hashes = {sha256(data) for data in files.values()}
        items = []
        for line in files[roles['records']].decode('utf-8').splitlines():
            row = json.loads(line)
            require(set(row) == {'record_id', 'symbol', 'kind', 'event_time', 'available_at', 'content',
                                 'source', 'raw_sha256', 'availability_basis', 'availability_estimated',
                                 'availability_evidence_sha256', 'availability_input', 'filing_accession', 'originating_record_id'}, 'unexpected record fields')
            require(row['kind'] in KINDS and row['symbol'] == task.symbol, 'record kind/instrument mismatch')
            require(type(row['availability_estimated']) is bool, 'availability uncertainty required')
            require(row['availability_basis'] in ('verified_release.v1', 'publication_date_next_open.v1',
                                                  'filing_date_next_open.v1'), 'unrecognized availability basis')
            require(row['availability_estimated'] == (row['availability_basis'] != 'verified_release.v1'),
                    'availability basis/uncertainty disagree')
            for field in ('raw_sha256', 'availability_evidence_sha256'):
                require(row[field] in bound_hashes, 'record evidence bytes not hash-bound')
            resolved = resolve_availability(row['availability_input'], next_exchange_open)
            require(resolved.at == datetime.fromisoformat(row['available_at'])
                    and resolved.basis == row['availability_basis']
                    and resolved.evidence_sha256 == row['availability_evidence_sha256'],
                    'compiled record differs from availability evidence')
            record = InformationRecord(row['record_id'], row['symbol'], datetime.fromisoformat(row['event_time']),
                                       datetime.fromisoformat(row['available_at']), row['content'], row['source'])
            if row['kind'] == 'ohlcv':
                bar = json.loads(row['content'])
                require(set(bar) == BAR_FIELDS, 'non-allowlisted bar field; construction metadata must stay private')
                require(record.event_time == record.available_at, 'bar available only at close')
            items.append(HistoryItem(record, row['kind']))
        require(len({i.record.record_id for i in items}) == len(items), 'duplicate immutable record identity')
        self._items = tuple(items)
        self._record_rows = tuple(json.loads(l) for l in files[roles['records']].decode().splitlines())
        records_by_id = {r['record_id']: r for r in self._record_rows}
        for row in self._record_rows:
            if row['kind'] == 'financial_fact':
                origin = records_by_id.get(row['originating_record_id'])
                require(origin is not None and origin['kind'] == 'filing'
                        and row['filing_accession'] == origin['filing_accession']
                        and row['available_at'] == origin['available_at'],
                        'financial fact must inherit originating document visibility')
        sessions = []
        for s in json.loads(files[roles['sessions']]):
            require(set(s) == {'opens_at', 'closes_at', 'raw_open', 'raw_close', 'actions'}, 'unexpected session fields')
            actions = []
            for a in s['actions']:
                if a['type'] == 'dividend_ex':
                    require(set(a) == {'type', 'id', 'amount_per_share'}, 'unexpected dividend fields')
                    actions.append(DividendEx(a['id'], Decimal(a['amount_per_share'])))
                elif a['type'] == 'dividend_pay':
                    require(set(a) == {'type', 'id'}, 'unexpected dividend fields')
                    actions.append(DividendPay(a['id']))
                else:
                    require(False, 'corporate action unsupported by current engine')
            sessions.append(TradingSession(task.symbol, datetime.fromisoformat(s['opens_at']),
                                           datetime.fromisoformat(s['closes_at']), Decimal(s['raw_open']),
                                           Decimal(s['raw_close']), corporate_actions=tuple(actions)))
        self._sessions = tuple(sessions)
        cal = exchange_calendar(task)
        cutoff_day = task.as_of.astimezone(ZoneInfo('America/New_York')).date().isoformat()
        require(cal.is_session(cutoff_day) and cal.session_close(cutoff_day).to_pydatetime() == task.as_of,
                'task clock differs from XNAS close')
        expected = [s for s in cal.sessions if s.date().isoformat() > cutoff_day][:task.horizon_sessions]
        require(len(sessions) == len(expected) == task.horizon_sessions, 'session horizon mismatch')
        for s, day in zip(sessions, expected):
            require(s.opens_at == cal.session_open(day).to_pydatetime()
                    and s.closes_at == cal.session_close(day).to_pydatetime(), 'session differs from XNAS calendar')
        # Formal coverage is checked against an embedded, pinned reference index.
        # Completion may be reviewed from an issuer mirror's file-set listing;
        # exact SEC transport/SGML is not required. Review claims remain explicit.
        if 'filings_history' not in self.blockers or 'filing_exhibits' not in self.blockers:
            coverage_path = admission.get('coverage_path')
            require(coverage_path in files and coverage_path not in roles.values(), 'coverage evidence missing')
            coverage = json.loads(files[coverage_path])
            require(coverage['schema_version'] == 'filing_coverage.v1', 'unsupported coverage evidence')
            reference_path = coverage['reference_path']
            require(reference_path in files, 'coverage reference index not bound')
            self.reference_digest = sha256(files[reference_path])
            reference = [json.loads(l) for l in files[reference_path].decode().splitlines()]
            require(reference and all(int(r['cik']) == coverage['cik'] for r in reference),
                    'coverage reference CIK mismatch')
            forms = {'10-K','10-K/A','10-Q','10-Q/A','8-K','8-K/A'}
            through = sessions[-1].closes_at.astimezone(ZoneInfo('America/New_York')).date().isoformat()
            expected_ids = {r['accessionNumber'] for r in reference
                            if r['form'] in forms and r['filingDate'] <= through}
            filings = coverage['filings']
            require(expected_ids and len({f['accession'] for f in filings}) == len(filings)
                    and {f['accession'] for f in filings} == expected_ids, 'coverage accessions missing or duplicated')
            record_rows = {r['record_id']:r for r in self._record_rows}
            for f in filings:
                require(f['primary_record_id'] in record_rows, 'filing primary record not queryable')
                primary = record_rows[f['primary_record_id']]
                require(primary['kind'] == 'filing' and primary['filing_accession'] == f['accession'],
                        'primary record accession mismatch')
                paths = f['file_paths']
                require(paths and len(set(paths)) == len(paths) and all(p in files for p in paths),
                        'filing file set missing')
                require(primary['raw_sha256'] in {sha256(files[p]) for p in paths},
                        'primary source absent from filing file set')
                if 'filing_exhibits' not in self.blockers:
                    require(f['attachments_complete'] is True and f['listing_evidence_path'] in files,
                            'attachment inventory evidence incomplete')

        if 'market_history_and_actions' not in self.blockers:
            previous = [day for day in cal.sessions if day.date().isoformat() <= cutoff_day][-252:]
            clocks = {i.record.available_at for i in items if i.kind == 'ohlcv'}
            require(len(previous) == 252 and all(cal.session_close(d).to_pydatetime() in clocks for d in previous),
                    'formal archive missing 252-session market warmup')
        # One bar for every decision clock; terminal close is never a policy input.
        bar_clocks = [i.record.available_at for i in items if i.kind == 'ohlcv']
        require(len(bar_clocks) == len(set(bar_clocks)), 'duplicate bar clock')
        require(all(clock in bar_clocks for clock in [task.as_of] + [s.closes_at for s in sessions[:-1]]),
                'missing decision-time bar')

    def scan(self, symbol, available_through):
        aware(available_through)
        return tuple(i for i in self._items if i.record.symbol == symbol
                     and i.record.available_at <= available_through)

    def snapshot(self, symbol, decision_time):
        visible = self.scan(symbol, decision_time)
        bars = [i.record for i in visible if i.kind == 'ohlcv' and i.record.event_time == decision_time]
        require(len(bars) == 1, 'exact decision-time bar required')
        # Historical document bodies are only exposed through paginated history tools.
        return snapshot_at(symbol, decision_time, tuple(bars))

    def next_sessions(self, task):
        require(task == self.task, 'session request differs from bound task')
        return self._sessions
