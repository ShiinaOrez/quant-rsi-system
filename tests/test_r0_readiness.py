from pathlib import Path
import tempfile
import unittest

from quant_rsi.domain.shared import DomainError
from scripts.r0_readiness import inventory, markdown


class InventoryTests(unittest.TestCase):
    def test_inventory_checks_bytes_instead_of_trusting_catalogue(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'raw').write_text('bytes')
            config={'single_documents':[{'accession':'a','scope':'primary_document','path':'raw','sha256':'a'*64}]}
            with self.assertRaisesRegex(DomainError,'integrity failure'):inventory(config,root)

    def test_empty_cache_is_not_described_as_no_document_exists(self):
        report={'eligible_cases':0,'case_count':20,'formal_r0_eligible':False,'cases':[
            {'task_id':'fixture','expected_six_form_filings':3,'acquired_any_scope':0,
             'market_window_audited':True,'latest_initial_filings':{'10-Q':{'acquired_scopes':[]}}}]}
        text=markdown(report)
        self.assertIn('未纳入来源清单',text)
        self.assertIn('不等于磁盘没有',text)
