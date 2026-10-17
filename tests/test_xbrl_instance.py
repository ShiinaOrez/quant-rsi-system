"""Tests for quant_rsi.adapters.xbrl_instance (traditional XBRL 2.1 instances) and its dispatcher.

Synthetic instances cover the lexical boundaries (negative, signs, whitespace, exponents, commas),
precision/decimals/nil rules, no rescaling, dimensions, duplicates, tuples/fractions, footnotes,
ZIP/XML safety and availability. Saved Intel instance ZIPs are checked when present.
"""
import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from quant_rsi.adapters import inline_xbrl as ix
from quant_rsi.adapters import xbrl_instance as xi

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "artifacts" / "benchmark-quality" / "intc-zip-inventory.json"
ACC = "0000000000-13-000001"
NS = ('xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
      'xmlns:us-gaap="http://fasb.org/us-gaap/2012" xmlns:iso4217="http://www.xbrl.org/2003/iso4217" '
      'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:link="http://www.xbrl.org/2003/linkbase" '
      'xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:t="http://example.com/t"')
ENTITY = '<xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000050863</xbrli:identifier>{seg}</xbrli:entity>'
SEG = ('<xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementBusinessSegmentsAxis">'
       't:FoundryMember</xbrldi:explicitMember></xbrli:segment>')


def ctx(cid, period, seg=""):
    return f'<xbrli:context id="{cid}">{ENTITY.format(seg=seg)}<xbrli:period>{period}</xbrli:period></xbrli:context>'


INSTANT = "<xbrli:instant>2012-12-29</xbrli:instant>"
RESOURCES = (ctx("c-d", "<xbrli:startDate>2012-01-01</xbrli:startDate><xbrli:endDate>2012-12-29</xbrli:endDate>")
             + ctx("c-i", INSTANT) + ctx("c-i2", INSTANT) + ctx("c-seg", INSTANT, SEG)
             + '<xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>'
             '<xbrli:unit id="usdPerShare"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso4217:USD'
             '</xbrli:measure></xbrli:unitNumerator><xbrli:unitDenominator><xbrli:measure>xbrli:shares'
             '</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>')


def instance(body, resources=RESOURCES, prolog=""):
    return (f'<?xml version="1.0" encoding="utf-8"?>{prolog}<xbrli:xbrl {NS}>'
            f'<link:schemaRef xlink:type="simple" xlink:href="t.xsd"/>{resources}{body}</xbrli:xbrl>').encode()


def num(value, concept="us-gaap:Assets", fid="f1", ctx_ref="c-i", unit="usd", extra='decimals="-6"'):
    return f'<{concept} id="{fid}" contextRef="{ctx_ref}" unitRef="{unit}" {extra}>{value}</{concept}>'


def make_zip(tmp, members, name="a.zip"):
    path = Path(tmp) / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for member, data in members.items():
            z.writestr(member, data)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def run_zip(self, members, **kw):
        return xi.extract_any_zip(make_zip(self.tmp, members), accession=ACC, **kw)

    def facts(self, body, **kw):
        result = self.run_zip({"t-20121229.xml": instance(body, **kw)})
        return {f["fact_id"]: f for d in result["documents"] for f in d["facts"]}, result


class Numbers(Base):
    def test_full_value_is_not_rescaled_by_decimals(self):
        facts, result = self.facts(num("66089000000", extra='decimals="-6"'))
        f = facts["f1"]
        self.assertEqual((f["value"], f["raw_text"], f["decimals"], f["scale"]), ("66089000000", "66089000000", "-6", None))
        self.assertEqual((f["kind"], f["status"], f["item_type"]), ("numericItem", "ok", None))
        self.assertEqual(f["source"]["member"], "t-20121229.xml")
        self.assertEqual(result["document_type"], "xbrl_instance")

    def test_lexical_forms(self):
        cases = {"-1234": "-1234", "+5": "5", "\n  12.50 \t": "12.5", "0.0490": "0.049", "-1200": "-1200", "000.100": "0.1", "-0": "0", ".5": "0.5", "7.": "7",
                 "0.000000000000000000000000001": "0.000000000000000000000000001"}
        body = "".join(num(raw, fid=f"f{i}", ctx_ref="c-i", concept=f"t:C{i}") for i, raw in enumerate(cases))
        facts, _ = self.facts(body)
        for i, expected in enumerate(cases.values()):
            self.assertEqual(facts[f"f{i}"]["value"], expected)

    def test_float_only_forms_stay_raw(self):
        body = "".join(num(raw, fid=f"f{i}", concept=f"t:C{i}") for i, raw in enumerate(["1.5E3", "-2e-2", "INF", "NaN"]))
        facts, result = self.facts(body)
        for f in facts.values():
            self.assertEqual((f["status"], f["value"]), ("raw_only", None))
            self.assertEqual(f["notes"], ["float_lexical_requires_float_or_double_type_unverified"])
        self.assertEqual(result["counts"]["numericItem"]["by_status"], {"raw_only": 4})

    def test_invalid_numbers_are_errors_with_raw_text(self):
        cases = {"1,000": "invalid_numeric_lexical", "(5)": "invalid_numeric_lexical", "5-": "invalid_numeric_lexical",
                 "": "empty_value", "1 000": "invalid_numeric_lexical", "$5": "invalid_numeric_lexical"}
        body = "".join(num(raw, fid=f"f{i}", concept=f"t:C{i}") for i, raw in enumerate(cases))
        facts, _ = self.facts(body)
        for i, (raw, code) in enumerate(cases.items()):
            f = facts[f"f{i}"]
            self.assertEqual((f["status"], f["value"], f["errors"], f["raw_text"]), ("error", None, [code], raw))

    def test_comment_inside_value_is_error(self):
        facts, _ = self.facts(num("12<!-- x -->34"))
        self.assertEqual(facts["f1"]["errors"], ["comment_inside_value"])

    def test_digit_limit(self):
        facts, _ = self.facts(num("9" * 61))
        self.assertEqual(facts["f1"]["errors"], ["too_many_digits"])


class Attributes(Base):
    def test_invalid_nil_is_not_a_valid_value(self):
        body = (num("1", fid="bad", extra='decimals="0" xsi:nil="yes"')
                + num("", fid="good", extra='xsi:nil=" true "')
                + '<us-gaap:Note id="note" contextRef="c-d" xsi:nil="yes">text</us-gaap:Note>')
        facts, _ = self.facts(body)
        for identity in ("bad", "note"):
            self.assertEqual(facts[identity]["status"], "error")
            self.assertIsNone(facts[identity]["value"])
            self.assertIn("invalid_xsi_nil", facts[identity]["errors"])
        self.assertEqual(facts["good"]["status"], "nil")

    def test_precision_decimals_and_nil_rules(self):
        body = (num("1", fid="both", extra='decimals="0" precision="3"')
                + num("1", fid="neither", extra="")
                + num("1", fid="prec0", extra='precision="0"')
                + num("1", fid="decinf", extra='decimals="INF"')
                + num("1", fid="baddec", extra='decimals="x"')
                + num("1", fid="badprec", extra='precision="-1"')
                + num("", fid="nil", extra='xsi:nil="true"')
                + num("", fid="nildec", extra='xsi:nil="true" decimals="0"')
                + num("4", fid="nilcontent", extra='xsi:nil="true"'))
        facts, _ = self.facts(body)
        self.assertEqual(facts["both"]["errors"], ["both_decimals_and_precision"])
        self.assertEqual(facts["neither"]["errors"], ["decimals_or_precision_required"])
        self.assertEqual((facts["prec0"]["value"], facts["prec0"]["precision"]), ("1", "0"))
        self.assertEqual(facts["decinf"]["decimals"], "INF")
        self.assertEqual(facts["baddec"]["errors"], ["invalid_decimals"])
        self.assertEqual(facts["badprec"]["errors"], ["invalid_precision"])
        self.assertEqual((facts["nil"]["status"], facts["nil"]["value"]), ("nil", None))
        self.assertEqual(facts["nildec"]["errors"], ["nil_with_decimals_or_precision"])
        self.assertEqual(facts["nilcontent"]["errors"], ["nil_with_content"])

    def test_non_numeric_text_is_kept_verbatim(self):
        text = "  &lt;p&gt;Note &amp; more&lt;/p&gt;\n "
        body = (f'<us-gaap:Note id="n1" contextRef="c-d">{text}</us-gaap:Note>'
                '<us-gaap:Year id="n2" contextRef="c-d">2012</us-gaap:Year>'
                '<us-gaap:Bad id="n3" contextRef="c-d" decimals="0">x</us-gaap:Bad>'
                '<us-gaap:Empty id="n4" contextRef="c-d" xsi:nil="true"/>')
        facts, result = self.facts(body)
        self.assertEqual(facts["n1"]["value"], "  <p>Note & more</p>\n ")
        # Digits without unitRef are not a number: the type is unknown.
        self.assertEqual((facts["n2"]["kind"], facts["n2"]["value"]), ("nonNumericItem", "2012"))
        self.assertEqual(facts["n3"]["errors"], ["decimals_or_precision_on_non_numeric"])
        self.assertEqual(facts["n4"]["status"], "nil")
        self.assertEqual(result["counts"]["nonNumericItem"]["total"], 4)

    def test_missing_and_ambiguous_references(self):
        body = (num("1", fid="noctx", ctx_ref="zz") + num("1", fid="nounit", unit="zz", concept="t:B")
                + num("1", fid="c-i2", concept="t:C"))
        facts, result = self.facts(body)
        self.assertEqual(facts["noctx"]["errors"], ["missing_context"])
        self.assertEqual(facts["nounit"]["errors"], ["missing_unit"])
        self.assertIn("duplicate_id", result["counts"]["issues_by_code"])


class Structure(Base):
    def test_unprefixed_unit_uses_declared_default_namespace(self):
        resources = RESOURCES.replace('>xbrli:shares<', '>shares<')
        body = num('1.25', unit='usdPerShare')
        for namespace in (ix.XBRLI, 'urn:custom:units'):
            with self.subTest(namespace=namespace):
                raw = instance(body, resources=resources).replace(
                    b'<xbrli:xbrl ', f'<xbrli:xbrl xmlns="{namespace}" '.encode(), 1)
                result = self.run_zip({'t-20121229.xml': raw})
                fact = result['documents'][0]['facts'][0]
                self.assertEqual((fact['status'], fact['value']), ('ok', '1.25'))
                measure = fact['unit']['denominator'][0]
                self.assertEqual((measure['namespace'], measure['local_name']), (namespace, 'shares'))

    def test_unprefixed_unit_without_binding_stays_error(self):
        resources = RESOURCES.replace('>xbrli:shares<', '>shares<')
        facts, result = self.facts(num('1.25', unit='usdPerShare'), resources=resources)
        self.assertEqual((facts['f1']['status'], facts['f1']['value']), ('error', None))
        self.assertEqual(facts['f1']['raw_text'], '1.25')
        self.assertIn('unbound_default_namespace', str(result['documents'][0]['units']['usdPerShare']['errors']))

    def test_local_default_rebinding_stays_conservative(self):
        resources = RESOURCES.replace('<xbrli:measure>xbrli:shares',
                                     '<xbrli:measure xmlns="urn:local">shares')
        raw = instance(num('1.25', unit='usdPerShare'), resources=resources).replace(
            b'<xbrli:xbrl ', f'<xbrli:xbrl xmlns="{ix.XBRLI}" '.encode(), 1)
        result = self.run_zip({'t-20121229.xml': raw})
        fact = result['documents'][0]['facts'][0]
        self.assertEqual(fact['status'], 'error')
        self.assertIn('namespace_default_rebound', str(result['documents'][0]['units']['usdPerShare']['errors']))

    def test_dimensions_units_periods(self):
        body = (num("10", fid="seg", ctx_ref="c-seg") + num("1.25", fid="eps", concept="us-gaap:EarningsPerShareBasic",
                                                            ctx_ref="c-d", unit="usdPerShare", extra='decimals="2"'))
        facts, result = self.facts(body)
        dim = facts["seg"]["context"]["segment"][0]
        self.assertEqual((dim["dimension"]["local_name"], dim["member"]["qname"]),
                         ("StatementBusinessSegmentsAxis", "t:FoundryMember"))
        self.assertEqual(facts["eps"]["unit"]["denominator"][0]["qname"], "xbrli:shares")
        self.assertEqual(facts["eps"]["context"]["period"], {"type": "duration", "start": "2012-01-01", "end": "2012-12-29"})
        self.assertEqual(result["counts"]["dimensional_contexts"], 1)

    def test_duplicates_are_grouped_never_chosen(self):
        body = (num("100", fid="a") + num("100", fid="b", ctx_ref="c-i2")
                + num("5", fid="c", concept="t:X") + num("6", fid="d", concept="t:X", ctx_ref="c-i2"))
        facts, result = self.facts(body)
        self.assertEqual(len(facts), 4)
        self.assertEqual(result["counts"]["duplicate_groups"], {"consistent": 1, "inconsistent": 1})

    def test_trailing_zero_spellings_are_one_value(self):
        facts, result = self.facts(num("0.0490", fid="a", extra='decimals="4"')
                                   + num("0.049", fid="b", ctx_ref="c-i2", extra='decimals="INF"'))
        self.assertEqual((facts["a"]["raw_text"], facts["a"]["value"]), ("0.0490", "0.049"))
        self.assertEqual(result["counts"]["duplicate_groups"], {"consistent": 1})

    def test_tuple_and_fraction_unsupported(self):
        body = ('<t:Tuple><t:Inner contextRef="c-i" unitRef="usd" decimals="0">1</t:Inner></t:Tuple>'
                '<t:Frac id="fr" contextRef="c-i" unitRef="usd"><xbrli:numerator>1</xbrli:numerator>'
                '<xbrli:denominator>3</xbrli:denominator></t:Frac>')
        facts, result = self.facts(body)
        self.assertEqual(list(facts), ["fr"])
        self.assertIn("unsupported_fraction", facts["fr"]["errors"])
        self.assertEqual(result["counts"]["issues_by_code"]["unsupported_tuple"], 1)

    def test_footnotes_attach_to_facts(self):
        body = (num("7", fid="f1") + '<link:footnoteLink xlink:type="extended" xlink:role="http://www.xbrl.org/2003/role/link">'
                '<link:loc xlink:type="locator" xlink:href="#f1" xlink:label="l1"/>'
                '<link:footnote xlink:type="resource" xlink:label="n1" xml:lang="en-US" '
                'xlink:role="http://www.xbrl.org/2003/role/footnote">Restated.</link:footnote>'
                '<link:footnoteArc xlink:type="arc" xlink:from="l1" xlink:to="n1" '
                'xlink:arcrole="http://www.xbrl.org/2003/arcrole/fact-footnote"/>'
                '<link:loc xlink:type="locator" xlink:href="#gone" xlink:label="l2"/>'
                '<link:footnoteArc xlink:type="arc" xlink:from="l2" xlink:to="n1"/></link:footnoteLink>')
        facts, result = self.facts(body)
        self.assertEqual([(n["text"], n["xml_lang"]) for n in facts["f1"]["footnotes"]], [("Restated.", "en-US")])
        self.assertIn("footnote_target_not_found", result["counts"]["issues_by_code"])


class Safety(Base):
    def test_doctype_and_entities_refused(self):
        bomb = instance(num("&x;"), prolog='<!DOCTYPE x [<!ENTITY x "1">]>')
        # Not classified as an instance (root_tag refuses DTDs) and never parsed into facts.
        with self.assertRaises(ix.ExtractionError) as cm:
            xi.extract_document(bomb, member="t.xml", provenance={})
        self.assertEqual(cm.exception.code, "doctype_refused")
        with self.assertRaises(ix.ExtractionError):
            self.run_zip({"t.xml": bomb})

    def test_unsafe_member_refused(self):
        result = self.run_zip({"t.xml": instance(num("1")), "../evil.xml": instance(num("2"))})
        self.assertEqual(result["counts"]["issues_by_code"].get("unsafe_member_name"), 1)
        self.assertEqual([d["member"] for d in result["documents"]], ["t.xml"])

    def test_multiple_instances_all_extracted_and_flagged(self):
        result = self.run_zip({"a.xml": instance(num("1")), "b.xml": instance(num("2"))})
        self.assertEqual(result["counts"]["instance_documents"], 2)
        self.assertEqual(result["counts"]["inline_documents"], 0)
        self.assertIn("multiple_instance_documents", result["counts"]["issues_by_code"])

    def test_linkbases_and_non_xbrl_xml_ignored(self):
        lab = b'<?xml version="1.0"?><link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase"/>'
        result = self.run_zip({"t.xml": instance(num("1")), "t_lab.xml": lab, "junk.xml": b"<<<"})
        self.assertEqual([d["member"] for d in result["documents"]], ["t.xml"])

    def test_mixed_inline_and_instance_refused(self):
        html = b'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"/>'
        with self.assertRaises(ix.ExtractionError) as cm:
            self.run_zip({"t.xml": instance(num("1")), "t.htm": html})
        self.assertEqual(cm.exception.code, "mixed_inline_and_instance_refused")

    def test_no_instance_reported(self):
        result = xi.extract_zip(make_zip(self.tmp, {"x.txt": b"hi"}), accession=ACC)
        self.assertEqual(result["counts"]["issues_by_code"], {"no_xbrl_instance_document": 1})

    def test_availability(self):
        facts, result = self.facts(num("1"))
        self.assertEqual((result["task_ready"], result["availability"]["available_at"], facts["f1"]["pit_ready"]),
                         (False, None, False))
        with self.assertRaises(ix.ExtractionError):
            self.run_zip({"t.xml": instance(num("1"))}, available_at="2013-02-19T16:00:00")
        result = self.run_zip({"t.xml": instance(num("1"))}, available_at="2013-02-19T16:00:00-05:00",
                              available_at_basis="test")
        self.assertEqual(result["task_ready"], False)
        self.assertTrue(result["documents"][0]["facts"][0]["pit_ready"])


class Script(Base):
    def test_cli_single_instance(self):
        spec = importlib.util.spec_from_file_location("extract_financials", ROOT / "scripts" / "extract_financials.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        path = make_zip(self.tmp, {"t.xml": instance(num("-1234000000", extra='decimals="-6"')
                                                     + num("5", concept="us-gaap:NetIncomeLoss", fid="ni", ctx_ref="c-d"))})
        out = Path(self.tmp) / "out"
        self.assertEqual(mod.main(["--zip", str(path), "--accession", ACC, "--out-dir", str(out)]), 0)
        summary = json.loads((out / "summary.json").read_text())
        filing = summary["filings"][ACC]
        self.assertEqual(filing["document_type"], "xbrl_instance")
        self.assertEqual(filing["counts"]["inline_documents"], 0)
        self.assertEqual({r["concept"]: r["value"] for r in filing["key_facts"]},
                         {"us-gaap:Assets": "-1234000000", "us-gaap:NetIncomeLoss": "5"})
        self.assertIn("numericItem", (out / "summary.md").read_text())


@unittest.skipUnless(INVENTORY.exists(), "Intel ZIP inventory not present")
class SavedIntel(unittest.TestCase):
    def _run(self, accession):
        filing = next(f for f in json.loads(INVENTORY.read_text())["filings"] if f["accession"] == accession)
        path = ROOT / filing["path"]
        if not path.exists():
            self.skipTest(f"{path} missing")
        return xi.extract_any_zip(path, accession=accession, source_url=filing["url"])

    def _value(self, result, local, period):
        values = {f["value"] for d in result["documents"] for f in d["facts"]
                  if f["concept"]["local_name"] == local and f["context"]["period"] == period
                  and not f["context"]["segment"] and not f["context"]["scenario"]}
        self.assertEqual(len(values), 1, values)
        return values.pop()

    def test_2011_10q(self):
        r = self._run("0000950123-11-074495")
        self.assertEqual(r["document_type"], "xbrl_instance")
        self.assertEqual(self._value(r, "Assets", {"type": "instant", "instant": "2011-07-02"}), "66089000000")

    def test_2015_10k_continuing_operations_cfo(self):
        # Values checked by hand against intc-20151226.xml lines 11309, 12557, 12562.
        r = self._run("0000050863-16-000105")
        fy = {"type": "duration", "start": "2014-12-28", "end": "2015-12-26"}
        self.assertEqual(self._value(r, "Assets", {"type": "instant", "instant": "2015-12-26"}), "103065000000")
        self.assertEqual(self._value(r, "NetIncomeLoss", fy), "11420000000")
        self.assertEqual(self._value(r, "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations", fy),
                         "19017000000")

    def test_2018_10k(self):
        r = self._run("0000050863-19-000007")
        self.assertEqual(self._value(r, "Assets", {"type": "instant", "instant": "2018-12-29"}), "127963000000")
        self.assertEqual(self._value(r, "NetIncomeLoss", {"type": "duration", "start": "2017-12-31",
                                                         "end": "2018-12-29"}), "21053000000")


if __name__ == "__main__":
    unittest.main()
