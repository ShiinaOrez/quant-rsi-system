"""Tests for quant_rsi.adapters.edgar_index (offline SEC filing index page parser).

Synthetic pages follow the EDGAR index layout and cover Accepted time zones (EDT/EST, DST fold
and gap, missing/conflicting/unparseable), identity checks, hostile links, truncation, duplicate
and conflicting rows, unknown sizes and derived markers. The saved AAPL capture is checked when
present against an independent regex reading of the same bytes.
"""
import hashlib
import json
import re
import unittest
from html import unescape
from pathlib import Path

from quant_rsi.adapters import edgar_index as ei

ROOT = Path(__file__).resolve().parents[1]
BATCH6 = ROOT / "artifacts" / "benchmark-quality" / "claude" / "batch-06"
SAMPLE = BATCH6 / "sources" / "wayback-aapl-0000320193-23-000077-index.html.body"
SAMPLE_HEADERS = BATCH6 / "sources" / "wayback-aapl-0000320193-23-000077-index.html.headers"
CIK, ACC = 320193, "0000320193-23-000077"
DIR = "/Archives/edgar/data/320193/000032019323000077"
URL = f"https://www.sec.gov{DIR}/{ACC}-index.html"
PROV = {"kind": "synthetic", "note": "unit test"}


def row(seq, desc, name, doc_type, size, href=None):
    href = href if href is not None else f"{DIR}/{name}"
    return (f'<tr><td scope="row">{seq}</td><td scope="row">{desc}</td>'
            f'<td scope="row"><a href="{href}">{name}</a></td>'
            f'<td scope="row">{doc_type}</td><td scope="row">{size}</td></tr>')


MAIN = [row(1, "10-Q", "aapl-20230701.htm", "10-Q", 963209,
            href=f"/ix?doc={DIR}/aapl-20230701.htm"),
        row(2, "EX-31.1", "ex311.htm", "EX-31.1", 10527),
        row("&nbsp;", "Complete submission text file", f"{ACC}.txt", "&nbsp;", 5939898)]
DATA = [row(3, "XBRL TAXONOMY EXTENSION SCHEMA DOCUMENT", "aapl-20230701.xsd", "EX-101.SCH", 38069),
        row(9, "<b>EXTRACTED</b> XBRL INSTANCE DOCUMENT", "aapl-20230701_htm.xml", "XML", 892947)]
HEAD = ('<tr><th scope="col"><acronym title="Sequence Number">Seq</acronym></th><th>Description</th>'
        '<th>Document</th><th>Type</th><th>Size</th></tr>')
FIELDS = [("Filing Date", "2023-08-04"), ("Accepted", "2023-08-03 18:04:43"), ("Documents", "9"),
          ("Period of Report", "2023-07-01")]


def table(name, rows, summary=True, close=True):
    attr = f' summary="{name}"' if summary else ""
    return (f'<div class="formDiv"><p>{name}</p><table class="tableFile"{attr}>{HEAD}'
            + "".join(rows) + ("</table></div>" if close else ""))


def page(fields=FIELDS, main=MAIN, data=DATA, acc=ACC, form="10-Q", company_cik="0000320193",
         ident_type="10-Q", extra_tables="", head_meta=""):
    info = "".join(f'<div class="infoHead">{k}</div><div class="info">{v}</div>' for k, v in fields)
    tables = (table("Document Format Files", main) if main is not None else "") + (
        table("Data Files", data) if data is not None else "") + extra_tables
    return (f'<html><head>{head_meta}<title>EDGAR Filing Documents for {acc}</title></head><body>'
            '<a href="https://www.sec.gov">SEC.gov</a><a href="/cgi-bin/browse-edgar?action=getcurrent">'
            'Latest</a><div id="formName"><strong>Form ' + form + '</strong> - Quarterly report</div>'
            f'<div id="secNum"><strong>SEC Accession No.</strong> {acc}</div>'
            f'<div class="formGrouping">{info}</div>'
            '<a href="/cgi-bin/viewer?action=view&amp;cik=320193" id="interactiveDataBtn">Interactive</a>'
            f'{tables}<!-- END DOCUMENT DIV --><div class="filerDiv"><div class="companyInfo">'
            f'<span class="companyName">Apple Inc. (Filer) <acronym>CIK</acronym>: '
            f'<a href="/cgi-bin/browse-edgar?CIK={company_cik}">{company_cik} (see all)</a></span>'
            f'<p class="identInfo">Type: <strong>{ident_type}</strong> | Act: <strong>34</strong></p>'
            '</div></div><!-- END FILER DIV --></body></html>').encode()


def parse(raw, url=URL, cik=CIK, acc=ACC, prov=PROV):
    return ei.parse_edgar_index(raw, original_index_url=url, expected_cik=cik,
                                expected_accession=acc, provenance=prov)


def codes(result, severity=None):
    return {i["code"] for i in result["issues"] if severity in (None, i["severity"])}


def with_field(name, value):
    return [(k, value if k == name else v) for k, v in FIELDS]


class AcceptedTime(unittest.TestCase):
    def test_baseline_summer_is_edt(self):
        result = parse(page())
        self.assertTrue(result["acceptance_metadata_ready"], result["issues"])
        self.assertEqual(result["accepted"]["value"], "2023-08-03T18:04:43-04:00")
        self.assertEqual(result["accepted"]["utc"], "2023-08-03T22:04:43Z")
        self.assertEqual(result["accepted"]["raw"], "2023-08-03 18:04:43")
        self.assertEqual(result["accepted"]["field"], "infoHead:Accepted")
        self.assertIsNone(result["available_at"])
        self.assertFalse(result["pit_ready"])
        self.assertEqual(result["availability_status"], "public_dissemination_not_verified")
        self.assertFalse(result["benchmark_ready"])

    def test_winter_is_est(self):
        fields = [("Filing Date", "2023-02-03"), ("Accepted", "2023-02-02 18:01:36"),
                  ("Documents", "9"), ("Period of Report", "2022-12-31")]
        result = parse(page(fields=fields))
        self.assertTrue(result["acceptance_metadata_ready"])
        self.assertEqual(result["accepted"]["value"], "2023-02-02T18:01:36-05:00")
        self.assertEqual(result["accepted"]["utc"], "2023-02-02T23:01:36Z")

    def test_transition_edges(self):
        cases = {"2023-03-12 01:59:59": "-05:00", "2023-03-12 03:00:00": "-04:00",
                 "2023-11-05 00:59:59": "-04:00", "2023-11-05 02:00:00": "-05:00"}
        for raw, offset in cases.items():
            value, problem = ei.eastern_accepted(raw)
            self.assertIsNone(problem, raw)
            self.assertTrue(value.isoformat().endswith(offset), (raw, value))

    def test_fold_and_gap_are_refused(self):
        for raw, code in (("2023-11-05 01:30:00", "accepted_ambiguous_dst_fold"),
                          ("2023-03-12 02:30:00", "accepted_nonexistent_local_time")):
            fields = [("Filing Date", raw[:10])] + FIELDS[1:]
            result = parse(page(fields=[(k, raw if k == "Accepted" else v) for k, v in fields]))
            self.assertIn(code, codes(result, ei.BLOCKING))
            self.assertIsNone(result["accepted"])
            self.assertFalse(result["acceptance_metadata_ready"])
            self.assertIsNone(result["available_at"])

    def test_missing_accepted_not_guessed_from_last_modified(self):
        meta = '<meta http-equiv="Last-Modified" content="Thu, 03 Aug 2023 22:04:43 GMT" />'
        result = parse(page(fields=[f for f in FIELDS if f[0] != "Accepted"], head_meta=meta))
        self.assertIn("accepted_missing", codes(result, ei.BLOCKING))
        self.assertIsNone(result["accepted"])
        self.assertFalse(result["acceptance_metadata_ready"])

    def test_conflicting_and_duplicate_accepted(self):
        conflict = parse(page(fields=FIELDS + [("Accepted", "2023-08-03 18:05:00")]))
        self.assertIn("conflicting_field", codes(conflict, ei.BLOCKING))
        self.assertNotIn("accepted_missing", codes(conflict))
        self.assertFalse(conflict["acceptance_metadata_ready"])
        same = parse(page(fields=FIELDS + [("Accepted", "2023-08-03 18:04:43")]))
        self.assertIn("duplicate_field", codes(same, ei.WARNING))
        self.assertTrue(same["acceptance_metadata_ready"])

    def test_non_page_formats_refused(self):
        for raw in ("2023-08-03T22:04:43Z", "2023-08-03 18:04", "2023-08-03", "2023-02-30 10:00:00",
                    "2023-08-03 18:04:43 EDT", ""):
            result = parse(page(fields=with_field("Accepted", raw)))
            self.assertIn("accepted_unparseable", codes(result, ei.BLOCKING), raw)
            self.assertFalse(result["acceptance_metadata_ready"])

    def test_filing_date_before_accepted_and_bad_dates(self):
        result = parse(page(fields=with_field("Filing Date", "2023-08-02")))
        self.assertIn("filing_date_before_accepted", codes(result, ei.BLOCKING))
        result = parse(page(fields=with_field("Filing Date", "2023/08/04")))
        self.assertIn("bad_date", codes(result, ei.BLOCKING))
        result = parse(page(fields=[f for f in FIELDS if f[0] != "Filing Date"]))
        self.assertIn("filing_date_missing", codes(result, ei.BLOCKING))
        result = parse(page(fields=[f for f in FIELDS if f[0] != "Period of Report"]))
        self.assertIn("period_of_report_missing", codes(result, ei.WARNING))
        self.assertTrue(result["acceptance_metadata_ready"])

    def test_orphan_info_head(self):
        raw = page().replace(b'<div class="infoHead">Documents</div>',
                             b'<div class="infoHead">Items</div><div class="infoHead">Documents</div>')
        self.assertIn("info_head_without_value", codes(parse(raw), ei.BLOCKING))


class Identity(unittest.TestCase):
    def test_url_identity_mismatches_raise(self):
        bad_urls = {
            "index_url_cik_mismatch": URL.replace("/320193/", "/789019/"),
            "index_url_accession_mismatch": URL.replace("000032019323000077", "000032019323000078"),
            "index_url_not_index_page": URL.replace("-index.html", ".txt"),
            "index_url_not_sec_archives": URL.replace("https://www.sec.gov", "https://evil.example"),
            "index_url_bad_characters": URL.replace("/Archives", "/%41rchives"),
        }
        for code, url in bad_urls.items():
            with self.assertRaisesRegex(ei.EdgarIndexError, code):
                parse(page(), url=url)
        for url in (URL.replace("https:", "http:"), URL + "?x=1", URL + "#a",
                    URL.replace("www.sec.gov", "user@www.sec.gov")):
            with self.assertRaises(ei.EdgarIndexError):
                parse(page(), url=url)
        self.assertTrue(parse(page(), url=URL.replace("/320193/", "/0000320193/"))["acceptance_metadata_ready"])

    def test_page_and_title_accession_must_match(self):
        other = "0000320193-23-000078"
        with self.assertRaisesRegex(ei.EdgarIndexError, "page_accession_mismatch"):
            parse(page().replace(f"{ACC}</div>".encode(), f"{other}</div>".encode()))
        with self.assertRaisesRegex(ei.EdgarIndexError, "title_accession_mismatch"):
            parse(page().replace(f"for {ACC}".encode(), f"for {other}".encode()))
        with self.assertRaisesRegex(ei.EdgarIndexError, "bad_expected"):
            parse(page(), acc="320193-23-77")
        with self.assertRaisesRegex(ei.EdgarIndexError, "bad_expected"):
            parse(page(), cik="AAPL")

    def test_agent_accession_prefix_differs_from_cik(self):
        acc = "0001193125-23-204201"
        nodash = acc.replace("-", "")
        d = f"/Archives/edgar/data/320193/{nodash}"
        main = [row(1, "8-K", "d8k.htm", "8-K", 100, href=f"{d}/d8k.htm"),
                row("", "Complete submission text file", f"{acc}.txt", "", 200, href=f"{d}/{acc}.txt")]
        raw = page(main=main, data=None, acc=acc, form="8-K", ident_type="8-K",
                   fields=with_field("Documents", "1"))
        result = parse(raw, url=f"https://www.sec.gov{d}/{acc}-index.htm", acc=acc)
        self.assertTrue(result["acceptance_metadata_ready"], result["issues"])
        self.assertFalse(result["identity"]["accession_prefix_is_expected_cik"])
        self.assertEqual(result["complete_submission_url"], f"https://www.sec.gov{d}/{acc}.txt")

    def test_expected_cik_not_on_page_and_form_conflict(self):
        result = parse(page(company_cik="0000789019"))
        self.assertIn("expected_cik_not_on_page", codes(result, ei.BLOCKING))
        result = parse(page(ident_type="10-K"))
        self.assertIn("form_type_conflict", codes(result, ei.BLOCKING))

    def test_rate_limit_or_error_page_is_not_an_index(self):
        for raw in (b"<html><head><title>SEC.gov | Request Rate Threshold Exceeded</title></head>"
                    b"<body><h1>Your Request Originates from an Undeclared Automated Tool</h1>"
                    b'<a href="/Archives/edgar/data/320193/000032019323000077/x.htm">x</a></body></html>',
                    b"", b"<html><body><table class='tableFile' summary='Other'></table></body></html>"):
            with self.assertRaisesRegex(ei.EdgarIndexError, "not_an_edgar_filing_index"):
                parse(raw)

    def test_input_contract(self):
        with self.assertRaisesRegex(ei.EdgarIndexError, "provenance_required"):
            parse(page(), prov={})
        with self.assertRaisesRegex(ei.EdgarIndexError, "not_utf8"):
            parse(page() + b"\xff")
        with self.assertRaisesRegex(ei.EdgarIndexError, "raw_not_bytes"):
            parse(page().decode())


class Links(unittest.TestCase):
    IDENTITY = ei.index_identity(URL, CIK, ACC)

    def test_accepted_forms(self):
        expect = f"https://www.sec.gov{DIR}/a.htm"
        for href, kind in ((f"{DIR}/a.htm", "direct"), (f"https://www.sec.gov{DIR}/a.htm", "direct"),
                           (f"//www.sec.gov{DIR}/a.htm", "direct"),
                           (f"/Archives/edgar/data/0000320193/000032019323000077/a.htm", "direct"),
                           (f"/ix?doc={DIR}/a.htm", "ix_wrapper")):
            self.assertEqual(ei.resolve_document_link(href, self.IDENTITY), (expect, "a.htm", kind))

    def test_hostile_links_rejected(self):
        hostile = {
            "https://evil.example/Archives/edgar/data/320193/000032019323000077/a.htm": "link_other_host",
            "//evil.example" + DIR + "/a.htm": "link_other_host",
            "http://www.sec.gov" + DIR + "/a.htm": "link_other_host",
            "javascript:alert(1)": "link_other_host",
            "https://user:pw@www.sec.gov" + DIR + "/a.htm": "link_encoded_or_credentials",
            "https://www.sec.gov@evil.example" + DIR + "/a.htm": "link_encoded_or_credentials",
            DIR + "/%2e%2e/%2e%2e/x.htm": "link_encoded_or_credentials",
            DIR + "/../000032019323000078/a.htm": "link_not_archives_path",
            DIR + "/./a.htm": "link_not_archives_path",
            DIR + "/..": "link_bad_filename",
            DIR + "/.hidden": "link_bad_filename",
            DIR + "\\..\\a.htm": "link_encoded_or_credentials",
            "/Archives/edgar/data/789019/000032019323000077/a.htm": "link_other_cik",
            "/Archives/edgar/data/320193/000032019323000078/a.htm": "link_other_accession",
            DIR + "/a.htm?download=1": "link_query",
            DIR + "/a.htm#frag": "link_fragment",
            "a.htm": "link_not_root_relative",
            "/ix?doc=" + DIR + "/a.htm&doc=" + DIR + "/b.htm": "ix_query_not_single_doc",
            "/ix?doc=" + DIR + "/a.htm&xbrl=1": "ix_query_not_single_doc",
            "/ix?file=" + DIR + "/a.htm": "ix_query_not_single_doc",
            "/ix?doc=https://evil.example/a.htm": "ix_doc_not_archives_path",
            "/ix?doc=//evil.example" + DIR + "/a.htm": "link_not_archives_path",
            "/ix?doc=" + DIR + "/../../x/a.htm": "link_not_archives_path",
            "/ix?doc=" + DIR + "/a%2Ehtm": "link_encoded_or_credentials",
            "/ix?doc=" + DIR + "/a.htm#x": "link_fragment",
            "/ix?doc=": "ix_doc_not_archives_path",
            "/ix?doc": "ix_query_malformed",
            "/ix?doc=" + DIR + "/a.htm;x": "link_bad_filename",
            "/ix.xhtml?doc=" + DIR + "/a.htm": "link_query",
            "/cgi-bin/viewer?action=view": "link_query",
            DIR + "/a b.htm": "link_bad_characters",
            DIR + "/aé.htm": "link_bad_characters",
            "": "link_bad_characters",
        }
        for href, code in hostile.items():
            with self.assertRaisesRegex(ei.EdgarIndexError, f"^{code}$", msg=href):
                ei.resolve_document_link(href, self.IDENTITY)

    def test_rejected_link_in_page_blocks(self):
        main = MAIN[:1] + [row(2, "EX-31.1", "ex311.htm", "EX-31.1", 10527,
                               href="https://evil.example/ex311.htm")] + MAIN[2:]
        result = parse(page(main=main))
        self.assertIn("rejected_link", codes(result, ei.BLOCKING))
        bad = [d for d in result["documents"] if d["name"] == "ex311.htm"][0]
        self.assertIsNone(bad["original_url"])
        self.assertEqual(bad["link_error"], "link_other_host")
        self.assertFalse(result["acceptance_metadata_ready"])

    def test_html_entities_decoded_before_validation(self):
        main = [MAIN[0].replace('href="/ix?doc=', 'href="/ix?doc&#61;')] + MAIN[1:]
        self.assertTrue(parse(page(main=main))["acceptance_metadata_ready"])
        main = [MAIN[0].replace(".htm\">", ".htm&amp;x=1\">")] + MAIN[1:]
        self.assertIn("rejected_link", codes(parse(page(main=main)), ei.BLOCKING))

    def test_anchor_text_must_match_link_and_single_anchor(self):
        main = [row(2, "EX-31.1", "ex311.htm", "EX-31.1", 1, href=f"{DIR}/other.htm")] + MAIN[2:]
        self.assertIn("link_name_mismatch", codes(parse(page(main=main)), ei.BLOCKING))
        two = MAIN[1].replace("</a>", f'</a><a href="{DIR}/b.htm">b.htm</a>')
        self.assertIn("document_link_count", codes(parse(page(main=[two] + MAIN[2:])), ei.BLOCKING))
        dup = MAIN[1].replace("<a href=", f'<a href="https://evil.example/x" href=')
        self.assertIn("document_link_href_count", codes(parse(page(main=[dup] + MAIN[2:]))))

    def test_navigation_links_are_not_documents(self):
        result = parse(page())
        self.assertEqual([d["name"] for d in result["documents"]],
                         ["aapl-20230701.htm", "ex311.htm", f"{ACC}.txt", "aapl-20230701.xsd",
                          "aapl-20230701_htm.xml"])
        unknown = parse(page(extra_tables=table("Related Links", [row(4, "x", "x.htm", "EX-99", 1)])))
        self.assertIn("unrecognized_table", codes(unknown, ei.BLOCKING))
        self.assertNotIn("x.htm", [d["name"] for d in unknown["documents"]])


class Rows(unittest.TestCase):
    def test_row_fields_and_roles(self):
        result = parse(page())
        docs = {d["name"]: d for d in result["documents"]}
        primary = docs["aapl-20230701.htm"]
        self.assertEqual((primary["sequence"], primary["type"], primary["size"], primary["role"],
                          primary["link_kind"]), (1, "10-Q", 963209, "primary", "ix_wrapper"))
        self.assertEqual(primary["original_url"], f"https://www.sec.gov{DIR}/aapl-20230701.htm")
        self.assertEqual(docs["ex311.htm"]["role"], "exhibit")
        self.assertEqual(docs["aapl-20230701.xsd"]["role"], "xbrl_schema_or_linkbase")
        self.assertEqual(docs["aapl-20230701.xsd"]["table"], "Data Files")
        extracted = docs["aapl-20230701_htm.xml"]
        self.assertEqual((extracted["derivation"], extracted["derivation_marker"]),
                         ("derived", "description_EXTRACTED"))
        self.assertEqual(docs["ex311.htm"]["derivation"], "not_marked")
        sgml = docs[f"{ACC}.txt"]
        self.assertEqual((sgml["role"], sgml["sequence"], sgml["sequence_raw"]),
                         ("complete_submission", None, ""))
        self.assertEqual(result["complete_submission_url"], f"https://www.sec.gov{DIR}/{ACC}.txt")
        self.assertEqual(result["counts"]["listed_rows"], 4)
        self.assertIn("declared_count_exceeds_listed", codes(result, ei.WARNING))
        self.assertEqual(result["enumeration"], "index_listed_only")

    def test_empty_size_is_unknown_and_bad_size_blocks(self):
        main = [row(2, "EX-31.1", "ex311.htm", "EX-31.1", "&nbsp;")] + MAIN[2:]
        result = parse(page(main=main))
        doc = result["documents"][0]
        self.assertEqual((doc["size"], doc["size_status"]), (None, "unknown"))
        self.assertTrue(result["acceptance_metadata_ready"], result["issues"])
        main = [row(2, "EX-31.1", "ex311.htm", "EX-31.1", "10,527")] + MAIN[2:]
        self.assertIn("bad_size", codes(parse(page(main=main)), ei.BLOCKING))
        main = [row("2a", "EX-31.1", "ex311.htm", "EX-31.1", 1)] + MAIN[2:]
        self.assertIn("bad_sequence", codes(parse(page(main=main)), ei.BLOCKING))

    def test_renderer_files_marked_derived(self):
        data = DATA + [row(10, "", "R2.htm", "XML", 1), row(11, "", "Financial_Report.xlsx", "XLSX", 2)]
        docs = {d["name"]: d for d in parse(page(data=data))["documents"]}
        for name in ("R2.htm", "Financial_Report.xlsx"):
            self.assertEqual(docs[name]["derivation_marker"], "edgar_renderer_filename")
        self.assertEqual(docs["aapl-20230701.xsd"]["derivation"], "not_marked")

    def test_duplicates_and_conflicts(self):
        same = parse(page(data=DATA + [DATA[0]]))
        self.assertIn("duplicate_row", codes(same, ei.WARNING))
        self.assertEqual(len([d for d in same["documents"] if d["sequence"] == 3]), 2)
        conflict = parse(page(data=DATA + [row(3, "other", "other.xsd", "EX-101.SCH", 5)]))
        self.assertIn("conflicting_sequence", codes(conflict, ei.BLOCKING))
        renamed = parse(page(data=DATA + [row(4, "XBRL", "aapl-20230701.xsd", "EX-101.SCH", 38069)]))
        self.assertIn("conflicting_document_name", codes(renamed, ei.BLOCKING))
        twice = parse(page(extra_tables=table("Data Files", [])))
        self.assertIn("duplicate_table", codes(twice, ei.BLOCKING))
        sgml2 = parse(page(main=MAIN + [row("", "Complete submission text file", f"{ACC}.txt", "", 1)]))
        self.assertIn("conflicting_complete_submission", codes(sgml2, ei.BLOCKING))

    def test_complete_submission_rules(self):
        result = parse(page(main=MAIN[:2]))
        self.assertIn("complete_submission_missing", codes(result, ei.BLOCKING))
        self.assertIsNone(result["complete_submission_url"])
        wrong = MAIN[:2] + [row("", "Complete submission text file", "other.txt", "", 1)]
        self.assertIn("complete_submission_mismatch", codes(parse(page(main=wrong)), ei.BLOCKING))

    def test_declared_count_lower_than_listed_blocks(self):
        result = parse(page(fields=with_field("Documents", "2")))
        self.assertIn("listed_exceeds_declared", codes(result, ei.BLOCKING))
        result = parse(page(fields=with_field("Documents", "many")))
        self.assertIn("bad_document_count", codes(result, ei.BLOCKING))

    def test_table_structure(self):
        short = MAIN[1].replace('<td scope="row">10527</td>', "")
        self.assertIn("malformed_row", codes(parse(page(main=[short] + MAIN[2:])), ei.BLOCKING))
        no_header = page().replace(HEAD.encode(), b"", 1)
        self.assertIn("unexpected_table_header", codes(parse(no_header), ei.BLOCKING))
        relabel = page().replace(b'summary="Data Files"', b'summary="Document Format Files"')
        self.assertIn("table_label_conflict", codes(parse(relabel), ei.BLOCKING))
        result = parse(page(main=None))
        self.assertIn("document_format_table_missing", codes(result, ei.BLOCKING))


class Truncation(unittest.TestCase):
    def test_truncated_inside_table(self):
        raw = page()
        cut = raw[:raw.index(b"aapl-20230701_htm.xml") + 5]
        result = parse(cut)
        found = codes(result, ei.BLOCKING)
        self.assertIn("unterminated_table", found)
        self.assertIn("layout_end_marker_missing", found)
        self.assertFalse(result["acceptance_metadata_ready"])
        self.assertEqual(result["accepted"]["raw"], "2023-08-03 18:04:43")

    def test_truncated_after_fields(self):
        raw = page()
        result = parse(raw[:raw.index(b"</div>", raw.index(b"Period of Report")) + 50])
        self.assertIn("document_format_table_missing", codes(result, ei.BLOCKING))
        self.assertFalse(result["acceptance_metadata_ready"])

    def test_missing_html_close_is_warning_only(self):
        result = parse(page().replace(b"</body></html>", b""))
        self.assertIn("no_html_close_tag", codes(result, ei.WARNING))
        self.assertTrue(result["acceptance_metadata_ready"])


def independent_rows(text):
    """Regex reading of the document tables, independent of the HTMLParser implementation."""
    rows = []
    for name, body in re.findall(r'<table class="tableFile" summary="([^"]+)">(.*?)</table>', text, re.S):
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            if not cells:
                continue
            clean = [" ".join(unescape(re.sub(r"<[^>]+>", "", c)).replace("\xa0", " ").split())
                     for c in cells]
            href = unescape(re.search(r'href="([^"]+)"', cells[2]).group(1))
            anchor = re.search(r">([^<]+)</a>", cells[2]).group(1)
            rows.append((name, clean[0], clean[1], anchor, clean[3], clean[4],
                         href.split("/")[-1]))
    return rows


@unittest.skipUnless(SAMPLE.exists(), "saved AAPL index capture not present")
class SavedAaplSample(unittest.TestCase):
    def setUp(self):
        self.raw = SAMPLE.read_bytes()
        link = re.search(r'<([^>]+)>; rel="original"', SAMPLE_HEADERS.read_text())
        self.url = link.group(1)
        self.result = ei.parse_edgar_index(
            self.raw, original_index_url=self.url, expected_cik=CIK, expected_accession=ACC,
            provenance={"raw_path": str(SAMPLE.relative_to(ROOT)), "link_original": self.url})

    def test_hash_matches_parent_verification(self):
        verified = json.loads((BATCH6 / "parent-verification.json").read_text())
        entry = [f for f in verified["files"] if f["file"] == SAMPLE.name][0]
        self.assertEqual(self.result["source"]["raw_sha256"], entry["raw_sha256"])
        self.assertEqual(self.result["source"]["raw_sha256"], hashlib.sha256(self.raw).hexdigest())

    def test_rows_match_independent_reading(self):
        expected = independent_rows(self.raw.decode())
        got = [(d["table"], d["sequence_raw"], d["description"], d["name"], d["type"], d["size_raw"],
                d["original_url"].rsplit("/", 1)[-1]) for d in self.result["documents"]]
        self.assertEqual(got, expected)
        self.assertGreater(len(expected), 0)
        for doc in self.result["documents"]:
            self.assertTrue(doc["original_url"].startswith(
                "https://www.sec.gov/Archives/edgar/data/320193/000032019323000077/"))

    def test_header_fields(self):
        r = self.result
        text = self.raw.decode()
        accepted_raw = re.search(r'infoHead">Accepted</div>\s*<div class="info">([^<]+)<', text).group(1)
        self.assertEqual(r["accepted"]["raw"], accepted_raw)
        self.assertEqual(r["accepted"]["value"], "2023-08-03T18:04:43-04:00")
        self.assertEqual(r["filing_date"]["value"], "2023-08-04")
        self.assertEqual(r["period_of_report"]["value"], "2023-07-01")
        self.assertEqual(r["form"]["value"], "10-Q")
        self.assertEqual(r["declared_document_count"]["value"], 69)
        self.assertLess(r["counts"]["listed_sequences"], 69)
        self.assertIn("declared_count_exceeds_listed", codes(r, ei.WARNING))
        self.assertEqual(r["complete_submission_url"].rsplit("/", 1)[-1], f"{ACC}.txt")
        self.assertTrue(r["acceptance_metadata_ready"], r["issues"])
        self.assertEqual(codes(r, ei.BLOCKING), set())


if __name__ == "__main__":
    unittest.main()
