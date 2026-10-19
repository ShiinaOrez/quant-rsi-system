from dataclasses import replace
from datetime import datetime, date, timedelta
import hashlib
import json
import importlib.util
from pathlib import Path
import tempfile
import unittest

from quant_rsi.application.availability import resolve_availability
from quant_rsi.application.history import PointInTimeHistory
from quant_rsi.adapters.frozen_archive import FrozenArchiveMarket, ADMISSION_CHECKS, sha256, next_exchange_open, exchange_calendar
from quant_rsi.adapters.toml_benchmark import load_definition
from quant_rsi.domain.shared import DomainError
from quant_rsi.domain.trading import TargetWeight
from quant_rsi.application.replay import replay
from quant_rsi.adapters.in_memory import ConstantWeightPolicy
from decimal import Decimal
from scripts.compile_archive import compile_bundle

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(importlib.util.find_spec('exchange_calendars'), 'optional market dependencies not installed')
class AvailabilityTests(unittest.TestCase):
    def test_weekend_and_dst_conservative_date_are_exchange_open(self):
        e = {'policy': 'filing_date_next_open.v1', 'filing_date': '2024-03-08', 'evidence_sha256': 'a'*64,
             'accepted_at': '2024-03-08T18:00:00-05:00'}
        r = resolve_availability(e, next_exchange_open)
        self.assertEqual(r.at, datetime.fromisoformat('2024-03-11T09:30:00-04:00'))
        self.assertTrue(r.estimated)
        for bad in [dict(policy='accepted_at.v1', accepted_at=e['accepted_at'], evidence_sha256='a'*64),
                    dict(e, accepted_at='2024-03-12T12:00:00-04:00')]:
            with self.assertRaises(DomainError):
                resolve_availability(bad, next_exchange_open)


class FrozenArchiveFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.task = replace(load_definition(ROOT/'benchmarks/tasks/intc-2024-07-11-60d.toml'), horizon_sessions=2)
        self.source = self.root/'raw.txt'
        self.source.write_text('immutable fixture source; not actual benchmark data')
        self.review = self.root/'review.txt'
        self.review.write_text('synthetic test evidence only')
        self.digest = sha256(self.source.read_bytes())
        self.records = []
        cal = exchange_calendar(self.task)
        days = [d.date().isoformat() for d in cal.sessions if d.date().isoformat() <= '2024-07-11'][-252:]
        for day in days + ['2024-07-12', '2024-07-15']:
            at = cal.session_close(day).to_pydatetime().isoformat()
            self.records.append(self.record('bar-'+day, 'ohlcv', at, json.dumps({
                'session':day,'close':100,'volume':10,'sma20':100,'sma60':90,'rsi14':50,'daily_volatility20':0}),
                {'policy':'verified_release.v1','published_at':at,'evidence_sha256':self.digest}))
        self.records.append(self.record('original','filing','2024-07-10T12:00:00-04:00','income 10 '+('x'*9000),
            {'policy':'filing_date_next_open.v1','filing_date':'2024-07-10','evidence_sha256':self.digest}))
        self.records.append(self.record('amendment','filing','2024-07-12T12:00:00-04:00','income 8',
            {'policy':'filing_date_next_open.v1','filing_date':'2024-07-12','evidence_sha256':self.digest}))
        self.sessions = [{'opens_at':d+'T09:30:00-04:00','closes_at':d+'T16:00:00-04:00',
                          'raw_open':'100','raw_close':'101','actions':[]} for d in ['2024-07-12','2024-07-15']]

    def record(self, rid, kind, event, content, availability):
        return {'record_id':rid,'kind':kind,'symbol':self.task.symbol,'event_time':event,'content':content,
                'source':'synthetic fixture','raw_sha256':self.digest,'availability_input':availability}

    def build(self, passed=False):
        admission = {'schema_version':'archive_admission.v1','checks':[
            {'code':code,'status':'passed' if passed else 'not_reviewed',
             'evidence_paths':['evidence/review.txt'] if passed else []} for code in sorted(ADMISSION_CHECKS)]}
        evidence = {'evidence/raw.txt':self.source,'evidence/review.txt':self.review}
        if passed:
            acc = '0000050863-24-000076'
            next(r for r in self.records if r['record_id']=='original')['filing_accession'] = acc
            reference = self.root/'reference.jsonl'
            reference.write_text(json.dumps({'cik':50863,'form':'10-Q','filingDate':'2024-04-26','accessionNumber':acc})+'\n')
            coverage = self.root/'coverage.json'
            coverage.write_text(json.dumps({'schema_version':'filing_coverage.v1','cik':50863,
                'reference_path':'evidence/reference.jsonl','filings':[{'accession':acc,
                'primary_record_id':'original','file_paths':['evidence/raw.txt'],'attachments_complete':True,
                'listing_evidence_path':'evidence/review.txt'}]}))
            evidence.update({'evidence/reference.jsonl':reference,'evidence/coverage.json':coverage})
            admission['coverage_path'] = 'evidence/coverage.json'
        return compile_bundle(self.task,self.records,self.sessions,admission,evidence,self.root/'bundle')

    def market(self, result, formal=False):
        return FrozenArchiveMarket(self.task,Path(result['manifest']),expected_sha256=result['sha256'],
                                   require_admission=formal)



@unittest.skipUnless(importlib.util.find_spec('exchange_calendars'), 'optional market dependencies not installed')
class FrozenArchiveTests(FrozenArchiveFixture, unittest.TestCase):
    def test_boundary_pagination_and_amendment_preserve_original(self):
        result=self.build();market=self.market(result)
        early=PointInTimeHistory(market,self.task.symbol,self.task.as_of)
        late=PointInTimeHistory(market,self.task.symbol,datetime.fromisoformat('2024-07-15T09:30:00-04:00'))
        self.assertEqual(early.search(kind='filing')['total'],1)
        first=late.search(kind='filing',limit=1);second=late.search(kind='filing',offset=first['next_offset'],limit=1)
        self.assertEqual([first['items'][0]['record_id'],second['items'][0]['record_id']],['amendment','original'])
        self.assertEqual(late.read('original')['content'],early.read('original')['content'])
        self.assertEqual(early.read('original',limit=8000)['next_offset'],8000)
        self.assertEqual(len(early.read('original',offset=8000)['content']),1010)
        before=PointInTimeHistory(market,self.task.symbol,datetime.fromisoformat('2024-07-15T09:29:59.999999-04:00'))
        for rid in ['amendment','missing']:
            with self.assertRaisesRegex(DomainError,'record not available'):before.read(rid)
        self.assertEqual(len(market.snapshot(self.task.symbol,self.task.as_of).records),1)
        self.assertNotIn('filing_inventory',market.snapshot(self.task.symbol,self.task.as_of).records[0].content)

    def test_diagnostic_compile_does_not_satisfy_formal_admission(self):
        result=self.build()
        self.assertFalse(result['task_promoted'])
        with self.assertRaisesRegex(DomainError,'admission blocked'):self.market(result,formal=True)
        report=replay(self.task,'fixture',ConstantWeightPolicy(TargetWeight(Decimal(0))),self.market(result),self.market(result))
        self.assertEqual(report.net_return,0)

    def test_pinned_manual_review_and_integrity_are_separate_checks(self):
        result=self.build(passed=True)
        self.market(result,formal=True)
        with (self.root/'bundle/records.jsonl').open('a') as f:f.write('{}\n')
        with self.assertRaisesRegex(DomainError,'integrity failure'):self.market(result,formal=True)

    def test_wrong_task_and_calendar_gap_fail(self):
        result=self.build()
        with self.assertRaisesRegex(DomainError,'task identity mismatch'):
            FrozenArchiveMarket(replace(self.task,symbol='AAPL'),Path(result['manifest']),expected_sha256=result['sha256'])
        # Re-signing an invalid session file still cannot bypass calendar checks.
        p=self.root/'bundle/sessions.json';sessions=json.loads(p.read_text());sessions[1]['opens_at']='2024-07-16T09:30:00-04:00';sessions[1]['closes_at']='2024-07-16T16:00:00-04:00';p.write_text(json.dumps(sessions))
        m=self.root/'bundle/manifest.json';x=json.loads(m.read_text());next(f for f in x['files'] if f['role']=='sessions')['sha256']=sha256(p.read_bytes());m.write_text(json.dumps(x));result['sha256']=sha256(m.read_bytes())
        with self.assertRaisesRegex(DomainError,'XNAS calendar'):self.market(result)

    def test_source_metadata_in_bar_and_forged_visibility_rejected(self):
        self.records[0]['content']=json.dumps({'session':'2024-07-11','future_split_factor':10})
        with self.assertRaisesRegex(DomainError,'non-allowlisted bar'):self.build()

    def test_loader_recomputes_visibility_instead_of_trusting_serialized_time(self):
        result=self.build();p=self.root/'bundle/records.jsonl';rows=[json.loads(l) for l in p.read_text().splitlines()];rows[-1]['available_at']=self.task.as_of.isoformat();p.write_text(''.join(json.dumps(r)+'\n' for r in rows));m=self.root/'bundle/manifest.json';x=json.loads(m.read_text());next(f for f in x['files'] if f['role']=='records')['sha256']=sha256(p.read_bytes());m.write_text(json.dumps(x));result['sha256']=sha256(m.read_bytes())
        with self.assertRaisesRegex(DomainError,'availability evidence'):self.market(result)

    def test_empty_coverage_cannot_be_promoted_by_passed_review_flags(self):
        result=self.build(passed=True)
        p=self.root/'bundle/evidence/coverage.json';coverage=json.loads(p.read_text());coverage['filings']=[];p.write_text(json.dumps(coverage))
        m=self.root/'bundle/manifest.json';x=json.loads(m.read_text());next(f for f in x['files'] if f['path']=='evidence/coverage.json')['sha256']=sha256(p.read_bytes());m.write_text(json.dumps(x));result['sha256']=sha256(m.read_bytes())
        with self.assertRaisesRegex(DomainError,'coverage accessions missing'):self.market(result,formal=True)

    def test_financial_fact_cannot_be_visible_before_its_origin(self):
        fact=self.record('fact','financial_fact','2024-03-30T00:00:00-04:00','Assets 100',
            {'policy':'verified_release.v1','published_at':'2024-07-10T12:00:00-04:00','evidence_sha256':self.digest})
        fact.update(originating_record_id='original',filing_accession=None)
        self.records.append(fact)
        with self.assertRaisesRegex(DomainError,'inherit originating document'):self.build()

    def test_history_search_pages_cover_more_than_fifty_records(self):
        for i in range(60):
            self.records.append(self.record('extra-'+str(i),'filing','2024-07-10T12:00:00-04:00','fixture '+str(i),
                {'policy':'filing_date_next_open.v1','filing_date':'2024-07-10','evidence_sha256':self.digest}))
        result=self.build();view=PointInTimeHistory(self.market(result),self.task.symbol,self.task.as_of)
        ids=[];offset=0
        while True:
            page=view.search(kind='filing',offset=offset,limit=20);ids.extend(r['record_id'] for r in page['items'])
            if page['next_offset'] is None:break
            offset=page['next_offset']
        self.assertEqual(len(ids),61);self.assertEqual(len(set(ids)),61)
