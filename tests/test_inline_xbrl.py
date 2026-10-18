"""Tests for quant_rsi.adapters.inline_xbrl and scripts/extract_financials.py.

Synthetic inline XBRL covers the distortion boundaries (scale/sign, nil, segments, units and
periods, footnote markup, unsupported transforms, duplicates, availability, namespaces, limits).
The two saved Intel 10-Q ZIPs are checked when present. Runs under pytest or unittest.
"""
import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from quant_rsi.adapters import inline_xbrl as ix

ROOT = Path(__file__).resolve().parents[1]
SAVED = ROOT / "artifacts" / "benchmark-quality" / "issuer-mirrors"
ACC = "0000000000-24-000001"
NS = ('xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" '
      'xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
      'xmlns:us-gaap="http://fasb.org/us-gaap/2023" xmlns:iso4217="http://www.xbrl.org/2003/iso4217" '
      'xmlns:ixt="http://www.xbrl.org/inlineXBRL/transformation/2020-02-12" '
      'xmlns:ixt-sec="http://www.sec.gov/inlineXBRL/transformation/2015-08-31" '
      'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:link="http://www.xbrl.org/2003/linkbase" '
      'xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:t="http://example.com/t"')
ENTITY = '<xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000050863</xbrli:identifier>{seg}</xbrli:entity>'
SEG = ('<xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementBusinessSegmentsAxis">'
       't:FoundryMember</xbrldi:explicitMember></xbrli:segment>')


def ctx(cid, period, seg=""):
    return f'<xbrli:context id="{cid}">{ENTITY.format(seg=seg)}<xbrli:period>{period}</xbrli:period></xbrli:context>'


INSTANT = "<xbrli:instant>2024-03-30</xbrli:instant>"
RESOURCES = (ctx("c-d", "<xbrli:startDate>2023-12-31</xbrli:startDate><xbrli:endDate>2024-03-30</xbrli:endDate>")
             + ctx("c-i", INSTANT) + ctx("c-i2", INSTANT) + ctx("c-seg", INSTANT, SEG)
             + '<xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>'
             '<xbrli:unit id="usdPerShare"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso4217:USD'
             '</xbrli:measure></xbrli:unitNumerator><xbrli:unitDenominator><xbrli:measure>xbrli:shares'
             '</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>')
SCHEMA = ('<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:link="http://www.xbrl.org/2003/linkbase" '
          'xmlns:xlink="http://www.w3.org/1999/xlink" targetNamespace="http://example.com/t">'
          '<xs:annotation><xs:appinfo>'
          '<link:linkbaseRef xlink:type="simple" xlink:href="t_pre.xml" '
          'xlink:role="http://www.xbrl.org/2003/role/presentationLinkbaseRef"/>'
          '<link:roleType roleURI="http://example.com/role/BS" id="BS">'
          '<link:definition>0000004 - Statement - Balance Sheets</link:definition></link:roleType>'
          '</xs:appinfo></xs:annotation><xs:element id="t_Custom" name="Custom"/></xs:schema>')
PRE = ('<link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:xlink="http://www.w3.org/1999/xlink">'
       '<link:presentationLink xlink:role="http://example.com/role/BS" xlink:type="extended">'
       '<link:loc xlink:type="locator" xlink:label="a" '
       'xlink:href="https://xbrl.fasb.org/us-gaap/2023/elts/us-gaap-2023.xsd#us-gaap_Assets"/>'
       '<link:loc xlink:type="locator" xlink:label="b" xlink:href="t.xsd#t_Custom"/>'
       '</link:presentationLink></link:linkbase>')


def nf(fid, name="us-gaap:Assets", text="100", c="c-i", unit="usd", extra='decimals="-6" scale="6"'):
    unit_attr = f' unitRef="{unit}"' if unit else ""
    return f'<ix:nonFraction id="{fid}" name="{name}" contextRef="{c}"{unit_attr} {extra}>{text}</ix:nonFraction>'


def page(body, prolog=""):
    return (f'<?xml version="1.0" encoding="utf-8"?>{prolog}<html {NS}><head><title>t</title></head><body>'
            f'<div style="display:none"><ix:header><ix:references><link:schemaRef xlink:type="simple" '
            f'xlink:href="t.xsd"/></ix:references><ix:resources>{RESOURCES}</ix:resources></ix:header></div>'
            f'<div>{body}</div></body></html>').encode()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def make_zip(self, members, name="t.zip"):
        path = Path(self.tmp.name) / name
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for member, data in members.items():
                z.writestr(member, data)
        return path

    def run_body(self, body, **kw):
        limits = kw.pop("limits", ix.Limits())
        path = self.make_zip({"t.htm": page(body), "t.xsd": SCHEMA, "t_pre.xml": PRE})
        result = ix.extract_zip(path, accession=ACC, source_url="file://synthetic", limits=limits, **kw)
        self.facts = {f["fact_id"]: f for d in result["documents"] for f in d["facts"]}
        return result

    def codes(self, result):
        return {i["code"] for i in result["issues"]}


class Numbers(Base):
    def test_registry_three_number_and_dash_boundaries(self):
        # Different namespace and grouping rules must not reuse the looser TR4 parser.
        for raw, expected in (("77,988", "77988"), ("1234.50", "1234.50"),
                              ("1\u00a0234.25", "1234.25"), ("1 234 567", "1234567")):
            self.assertEqual(ix._tr3_numdotdecimal(raw), ix.Decimal(expected))
        for raw in ("12,34", "1,", "1.2 3", "-1", "+1", "1e3", ".5", ""):
            with self.assertRaises(ix.TransformError):
                ix._tr3_numdotdecimal(raw)
        for dash in "-\u058a\u05be\u2010\u2011\u2012\u2013\u2014\u2015\ufe58\ufe63\uff0d":
            self.assertEqual(ix._tr3_zerodash(" " + dash + "\t"), 0)
        for raw in ("", "--", "0", "\u2212"):
            with self.assertRaises(ix.TransformError):
                ix._tr3_zerodash(raw)
        body = nf("tr3", text="77,988", extra='decimals="-6" scale="6" sign="-" format="ixt:numdotdecimal"')
        path = self.make_zip({"t.htm": page(body).replace(ix.TR4.encode(), ix.TR3.encode())})
        result = ix.extract_zip(path, accession=ACC)
        self.assertEqual(result["documents"][0]["facts"][0]["value"], "-77988000000")

    def test_million_scale_sign_and_decimals(self):
        self.run_body(
            nf("f1", "us-gaap:NetIncomeLoss", "1,069", "c-d",
               extra='decimals="-6" scale="6" sign="-" format="ixt:num-dot-decimal"')
            + nf("f2", text="4.8", extra='decimals="-8" scale="9" format="ixt:num-dot-decimal"')
            + nf("f3", "us-gaap:Rate", "25", "c-d", unit="usdPerShare", extra='decimals="2" scale="-2"')
            + nf("f4", "us-gaap:EarningsPerShareBasic", "0.09", "c-d", unit="usdPerShare",
                 extra='decimals="2" sign="-"')
            + nf("f5", "us-gaap:Other", "—", "c-d", extra='decimals="-6" scale="6" sign="-" format="ixt:fixed-zero"'))
        f = self.facts
        self.assertEqual(f["f1"]["value"], "-1069000000")
        self.assertEqual((f["f1"]["raw_text"], f["f1"]["scale"], f["f1"]["sign"], f["f1"]["decimals"]),
                         ("1,069", "6", "-", "-6"))
        self.assertEqual(f["f2"]["value"], "4800000000")
        self.assertEqual(f["f3"]["value"], "0.25")
        self.assertEqual(f["f4"]["value"], "-0.09")
        self.assertEqual(f["f5"]["value"], "0")
        self.assertEqual(f["f1"]["concept"], {"qname": "us-gaap:NetIncomeLoss", "namespace": "http://fasb.org/us-gaap/2023",
                                              "local_name": "NetIncomeLoss"})

    def test_nil_has_no_value_and_nil_with_content_is_error(self):
        self.run_body('<ix:nonFraction id="n1" name="us-gaap:CommitmentsAndContingencies" contextRef="c-i" '
                      'unitRef="usd" xsi:nil="true"/>'
                      '<ix:nonFraction id="n2" name="us-gaap:CommitmentsAndContingencies" contextRef="c-d" '
                      'unitRef="usd" xsi:nil="true">5</ix:nonFraction>')
        self.assertEqual((self.facts["n1"]["status"], self.facts["n1"]["value"]), ("nil", None))
        self.assertIn("nil_with_content", self.facts["n2"]["errors"])
        self.assertIsNone(self.facts["n2"]["value"])

    def test_invalid_numbers_are_errors_not_guesses(self):
        result = self.run_body(nf("a", text="-5", extra='decimals="0" format="ixt:num-dot-decimal"')
                               + nf("b", c="c-d", text="1.2.3", extra='decimals="0" format="ixt:num-dot-decimal"')
                               + nf("c", "us-gaap:X", text="(5)", extra='decimals="0"')
                               + nf("d", "us-gaap:Y", text="5", extra='scale="6"'))
        self.assertEqual(self.facts["a"]["errors"], ["invalid_transform_input"])
        self.assertEqual(self.facts["b"]["errors"], ["invalid_transform_input"])
        self.assertEqual(self.facts["c"]["errors"], ["invalid_number_without_format"])
        self.assertEqual(self.facts["d"]["errors"], ["decimals_or_precision_required"])
        self.assertTrue(all(f["value"] is None and f["raw_text"] for f in self.facts.values()))
        self.assertEqual(result["counts"]["nonFraction"]["by_status"], {"error": 4})

    def test_unsupported_transforms_keep_raw_text_and_error(self):
        result = self.run_body(
            nf("u1", text="1.069,5", extra='decimals="0" format="ixt:num-comma-decimal"')
            + nf("u2", "us-gaap:LineOfCredit", "five hundred", "c-d", extra='decimals="-6" format="ixt-sec:numwordsen"')
            + nf("u3", "us-gaap:CommercialPaper", "None", "c-d", extra='decimals="-6" format="ixt-sec:numwordsen"')
            + nf("u4", "us-gaap:Z", "7", "c-d", extra='decimals="0" format="foo:bar"'))
        self.assertEqual((self.facts["u1"]["errors"], self.facts["u1"]["raw_text"]), (["unsupported_format"], "1.069,5"))
        self.assertEqual(self.facts["u2"]["errors"], ["unsupported_transform_input"])
        self.assertEqual(self.facts["u3"]["value"], "0")
        self.assertEqual(self.facts["u4"]["errors"], ["format_unbound_prefix"])
        self.assertLessEqual({"unsupported_format", "unsupported_transform_input", "format_unbound_prefix"},
                             self.codes(result))

    def test_atomic_number_words_apply_scale_sign_and_keep_source_text(self):
        self.run_body(nf("word", "us-gaap:LineOfCredit", "  TwO\n", "c-d",
                         extra='decimals="0" scale="3" sign="-" format="ixt-sec:numwordsen"')
                      + nf("tens", "us-gaap:Other", "FORTY", "c-i",
                           extra='decimals="0" format="ixt-sec:numwordsen"')
                      + nf("teen", "us-gaap:Count", "nineteen", "c-i",
                           extra='decimals="0" format="ixt-sec:numwordsen"'))
        self.assertEqual((self.facts["word"]["status"], self.facts["word"]["value"]), ("ok", "-2000"))
        self.assertEqual(self.facts["word"]["raw_text"], "  TwO\n")
        self.assertEqual(self.facts["tens"]["value"], "40")
        self.assertEqual(self.facts["teen"]["value"], "19")

    def test_number_word_subset_does_not_guess_compounds_or_other_formats(self):
        words = ["twenty-one", "one hundred", "two and", "fourty", "2", "cinq", "one,", "two dollars"]
        self.run_body("".join(nf(f"bad{i}", f"us-gaap:Word{i}", word, "c-i",
                                extra='decimals="0" format="ixt-sec:numwordsen"')
                              for i, word in enumerate(words))
                      + nf("plainword", "us-gaap:Plain", "two", "c-i", extra='decimals="0"'))
        for i, word in enumerate(words):
            fact = self.facts[f"bad{i}"]
            self.assertEqual((fact["value"], fact["errors"], fact["raw_text"]),
                             (None, ["unsupported_transform_input"], word))
        self.assertEqual(self.facts["plainword"]["errors"], ["invalid_number_without_format"])

    def test_nested_nonfraction_yields_two_facts(self):
        inner = nf("in", "us-gaap:LineOfCredit", "no", "c-i", extra='decimals="-6" format="ixt-sec:numwordsen"')
        self.run_body(f'<ix:nonFraction id="out" name="us-gaap:LineOfCredit" contextRef="c-d" unitRef="usd" '
                      f'decimals="-6" format="ixt-sec:numwordsen">{inner}</ix:nonFraction>')
        self.assertEqual((self.facts["out"]["value"], self.facts["in"]["value"]), ("0", "0"))
        self.assertEqual(self.facts["in"]["nested_in_fact_id"], "out")
        bad = nf("in2", text="5", extra='decimals="0" scale="3"')
        self.run_body(f'<ix:nonFraction id="out2" name="us-gaap:Assets" contextRef="c-d" unitRef="usd" '
                      f'decimals="0">{bad}</ix:nonFraction>')
        self.assertIn("nested_property_mismatch", self.facts["out2"]["errors"])


class Structure(Base):
    def test_segment_and_consolidated_contexts_stay_separate(self):
        result = self.run_body(nf("cons", text="100") + nf("seg", text="30", c="c-seg"))
        cons, seg = self.facts["cons"], self.facts["seg"]
        self.assertEqual((cons["value"], seg["value"]), ("100000000", "30000000"))
        self.assertEqual(cons["context"]["segment"], [])
        self.assertEqual(seg["context"]["segment"][0]["member"]["namespace"], "http://example.com/t")
        self.assertEqual(result["documents"][0]["duplicates"], [])

    def test_units_and_periods(self):
        self.run_body(nf("eps", "us-gaap:EarningsPerShareBasic", "0.10", "c-d", unit="usdPerShare", extra='decimals="2"')
                      + nf("bal", text="1"))
        eps, bal = self.facts["eps"], self.facts["bal"]
        self.assertEqual([m["local_name"] for m in eps["unit"]["numerator"]], ["USD"])
        self.assertEqual([m["qname"] for m in eps["unit"]["denominator"]], ["xbrli:shares"])
        self.assertEqual(eps["context"]["period"], {"type": "duration", "start": "2023-12-31", "end": "2024-03-30"})
        self.assertEqual(bal["context"]["period"], {"type": "instant", "instant": "2024-03-30"})
        self.assertEqual(bal["unit"]["denominator"], [])

    def test_footnote_markup_and_exclude_never_become_numbers(self):
        result = self.run_body(
            nf("fn", text="1,069<sup>(1)</sup>", extra='decimals="-6" scale="6" format="ixt:num-dot-decimal"')
            + nf("ex", "us-gaap:Debt", '<ix:exclude>2</ix:exclude>', "c-d", extra='decimals="0"')
            + '<ix:nonNumeric id="t" name="us-gaap:Policy" contextRef="c-d">Text <ix:exclude>[note 9]</ix:exclude>'
              'kept</ix:nonNumeric>')
        self.assertEqual((self.facts["fn"]["value"], self.facts["fn"]["errors"]), (None, ["invalid_nonfraction_content"]))
        self.assertEqual(self.facts["fn"]["raw_text"], "1,069(1)")
        self.assertIsNone(self.facts["ex"]["value"])
        self.assertEqual(self.facts["t"]["value"], "Text kept")
        self.assertIn("invalid_nonfraction_content", self.codes(result))

    def test_conflicts_reported_and_consistent_duplicates_grouped(self):
        result = self.run_body(nf("a1", text="100") + nf("a2", text="101", c="c-i2")
                               + nf("e1", "us-gaap:Liabilities", "7") + nf("e2", "us-gaap:Liabilities", "7"))
        groups = {g["concept"]: g for g in result["documents"][0]["duplicates"]}
        self.assertEqual(groups["us-gaap:Assets"]["status"], "inconsistent")
        self.assertEqual(groups["us-gaap:Assets"]["context_refs"], ["c-i", "c-i2"])
        self.assertEqual(sorted(f["value"] for f in groups["us-gaap:Assets"]["facts"]), ["100000000", "101000000"])
        self.assertEqual(groups["us-gaap:Liabilities"]["status"], "consistent")
        self.assertEqual(self.facts["a1"]["value"], "100000000")   # both kept, nothing overwritten
        self.assertEqual(self.facts["a2"]["value"], "101000000")
        self.assertEqual(result["counts"]["duplicate_groups"], {"consistent": 1, "inconsistent": 1})

    def test_missing_context_and_unit(self):
        self.run_body(nf("m1", c="nope") + nf("m2", unit=None))
        self.assertEqual(self.facts["m1"]["errors"], ["missing_context"])
        self.assertEqual(self.facts["m2"]["errors"], ["missing_unit"])

    def test_namespace_rebinding_rejected(self):
        result = self.run_body('<span xmlns:us-gaap="http://evil.example/ns">' + nf("r") + "</span>")
        self.assertIn("concept_namespace_prefix_rebound", self.facts["r"]["errors"])
        self.assertIsNone(self.facts["r"]["value"])
        self.assertIn("namespace_prefix_rebound", self.codes(result))

    def test_roles_bound_by_concept_with_basis(self):
        self.run_body(nf("a") + '<ix:nonNumeric id="c" name="t:Custom" contextRef="c-d">x</ix:nonNumeric>'
                      + nf("o", "us-gaap:Other", "1"))
        roles = self.facts["a"]["presentation_roles"]
        self.assertEqual(roles, [{"role_uri": "http://example.com/role/BS",
                                  "definition": "0000004 - Statement - Balance Sheets",
                                  "basis": "external_fragment_convention_unverified"}])
        self.assertEqual(self.facts["c"]["presentation_roles"][0]["basis"], "local_schema_element_id")
        self.assertEqual(self.facts["o"]["presentation_roles"], [])


class Availability(Base):
    def test_unbound_available_at_is_not_pit_ready(self):
        result = self.run_body(nf("a"))
        self.assertIsNone(self.facts["a"]["available_at"])
        self.assertFalse(self.facts["a"]["pit_ready"])
        self.assertFalse(result["availability"]["pit_ready"])
        self.assertFalse(result["task_ready"])
        self.assertIsNone(result["availability"]["accepted_at"])

    def test_explicit_available_at_requires_timezone_and_basis(self):
        with self.assertRaises(ix.ExtractionError) as err:
            self.run_body(nf("a"), available_at="2024-04-26T16:05:00", available_at_basis="x")
        self.assertEqual(err.exception.code, "available_at_requires_timezone")
        with self.assertRaises(ix.ExtractionError):
            self.run_body(nf("a"), available_at="2024-04-26T16:05:00-04:00")
        result = self.run_body(nf("a") + nf("bad", c="nope"), available_at="2024-04-26T16:05:00-04:00",
                               available_at_basis="test fixture")
        self.assertTrue(self.facts["a"]["pit_ready"])
        self.assertFalse(self.facts["bad"]["pit_ready"])
        self.assertFalse(result["task_ready"])


class Refusals(Base):
    def test_corrupt_local_zip_header_is_explicit_failure(self):
        path = self.make_zip({"t.htm": page(nf("a"))})
        data = path.read_bytes()
        self.assertEqual(data[:4], b"PK\x03\x04")
        path.write_bytes(b"BAD!" + data[4:])
        with self.assertRaises(ix.ExtractionError) as error:
            ix.extract_zip(path, accession=ACC)
        self.assertEqual(error.exception.code, "corrupt_zip_member")

    def test_doctype_and_entities_refused(self):
        path = self.make_zip({"t.htm": page(nf("a"), prolog='<!DOCTYPE html [<!ENTITY x "1">]>')})
        with self.assertRaises(ix.ExtractionError) as err:
            ix.extract_zip(path, accession=ACC)
        self.assertEqual(err.exception.code, "doctype_refused")

    def test_zip_limits_external_and_unsafe_names(self):
        result = self.run_body(nf("a"), limits=ix.Limits(max_member_bytes=200))
        self.assertIn("member_too_large", self.codes(result))
        self.assertIn("no_inline_xbrl_document", self.codes(result))
        self.assertEqual(result["counts"]["facts"], 0)
        with self.assertRaises(ix.ExtractionError) as err:
            self.run_body(nf("a"), limits=ix.Limits(max_zip_bytes=10))
        self.assertEqual(err.exception.code, "zip_too_large")
        with self.assertRaises(ix.ExtractionError) as err:
            ix.extract_zip("https://example.com/x.zip", accession=ACC)
        self.assertEqual(err.exception.code, "external_source_refused")
        path = self.make_zip({"../evil.htm": page(nf("a"))}, "u.zip")
        result = ix.extract_zip(path, accession=ACC)
        self.assertIn("unsafe_member_name", self.codes(result))
        self.assertEqual(result["counts"]["facts"], 0)

    def test_inline_document_set_and_bad_accession_refused(self):
        path = self.make_zip({"a.htm": page(nf("a")), "b.htm": page(nf("b"))})
        with self.assertRaises(ix.ExtractionError) as err:
            ix.extract_zip(path, accession=ACC)
        self.assertEqual(err.exception.code, "inline_document_set_unsupported")
        with self.assertRaises(ix.ExtractionError):
            ix.extract_zip(path, accession="latest")


ZIPS = {"0000050863-24-000076": ("0000050863-24-000076-xbrl.zip", 754),
        "0000050863-24-000124": ("0000050863-24-000124-xbrl.zip", 1047)}


@unittest.skipUnless(all((SAVED / name).exists() for name, _ in ZIPS.values()), "saved Intel ZIPs absent")
class RealIntel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        receipts = {r["name"]: r for r in json.loads((SAVED / "INTC-xbrl-receipts.json").read_text())}
        cls.results = {acc: ix.extract_zip(SAVED / name, accession=acc, source_url=receipts[name]["url"])
                       for acc, (name, _) in ZIPS.items()}
        cls.receipts = receipts

    def fact(self, acc, fid):
        return next(f for f in self.results[acc]["documents"][0]["facts"] if f["fact_id"] == fid)

    def test_counts_and_no_silent_numbers(self):
        for acc, (name, total) in ZIPS.items():
            result = self.results[acc]
            self.assertEqual(result["zip"]["sha256"], self.receipts[name]["sha256"])
            self.assertEqual(result["counts"]["facts"], total)
            self.assertEqual(result["counts"]["pit_ready_facts"], 0)
            self.assertFalse(result["task_ready"])
            for fact in result["documents"][0]["facts"]:
                if fact["kind"] == "nonFraction":
                    self.assertTrue(fact["value"] is not None or fact["nil"] or fact["errors"], fact["fact_id"])

    def test_key_magnitudes(self):
        a = "0000050863-24-000076"
        self.assertEqual(self.fact(a, "f-110")["value"], "191572000000")      # Assets 2023-12-30
        self.assertEqual(self.fact(a, "f-109")["value"], "192733000000")      # Assets 2024-03-30
        self.assertEqual(self.fact(a, "f-177")["value"], "-1223000000")       # operating cash flow Q1 2024
        self.assertEqual(self.fact(a, "f-58")["value"], "-381000000")         # NetIncomeLoss Q1 2024
        self.assertEqual(self.fact(a, "f-377")["context"]["segment"][0]["dimension"]["local_name"], "PledgedStatusAxis")
        b = self.fact("0000050863-24-000124", "f-165")
        self.assertEqual((b["value"], b["concept"]["namespace"]), ("206205000000", "http://fasb.org/us-gaap/2024"))

    def test_cli_writes_reports(self):
        spec = importlib.util.spec_from_file_location("extract_financials", ROOT / "scripts" / "extract_financials.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        with tempfile.TemporaryDirectory() as out:
            cli.main(["--receipts", str(SAVED / "INTC-xbrl-receipts.json"), "--out-dir", out]
                     + [arg for name, _ in ZIPS.values() for arg in ("--zip", str(SAVED / name))])
            summary = json.loads((Path(out) / "summary.json").read_text())
            self.assertFalse(summary["availability"]["task_ready"])
            self.assertEqual(summary["cross_filing_exact_qname"]["shared_keys"], 0)
            self.assertGreater(summary["cross_filing"]["shared_keys"], 0)


if __name__ == "__main__":
    unittest.main()
