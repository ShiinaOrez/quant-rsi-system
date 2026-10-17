"""Tests for quant_rsi.adapters.hf_edgar and scripts.hf_edgar_sources (no network, no duckdb).

The filer object copies the MSFT FY2022 10-K metadata seen in the pinned dataset; the saved Hub
manifest and footer probe are checked when present. A fake reader stands in for DuckDB.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from quant_rsi.adapters import hf_edgar as hf
from scripts import hf_edgar_sources as cli

ROOT = Path(__file__).resolve().parents[1]
HF_DIR = ROOT / "artifacts" / "benchmark-quality" / "hf-edgar"
FOOTER = HF_DIR / "adapter-batch" / "probe" / "footer-015a22bc-129.json"
PATH = "10-K/015a22bc-640f-4e63-9274-710902d39f96-129.parquet"
ACC = "0001564590-22-026876"
FILER = {
    "company-data": {"conformed-name": "MICROSOFT CORP", "cik": "0000789019",
                     "assigned-sic": "7372", "fiscal-year-end": "0630"},
    "filing-values": {"form-type": "10-K", "act": "34", "file-number": "001-37845",
                      "film-number": "221115247"}}
REF_ROW = {"accessionNumber": ACC, "cik": 789019, "form": "10-K", "filingDate": "2022-07-28",
           "acceptanceDateTime": "2022-07-28T20:06:19+00:00"}
REFERENCE = {"accession": ACC, "cik": 789019, "form": "10-K", "filing_date": "2022-07-28"}


def manifest_bytes(sha=hf.REVISION, paths=(PATH, "10-Q/a-1.parquet"), **extra):
    data = {"id": hf.REPO, "sha": sha, "private": False, "gated": False,
            "siblings": [{"rfilename": "README.md"}] + [{"rfilename": p} for p in paths]}
    data.update(extra)
    return json.dumps(data).encode()


def meta_row(number=1214, **changes):
    row = {"metadata_accession-number": ACC, "metadata_filing-date": "20220728",
           "metadata_documents": None, "metadata_period": "20220630",
           "metadata_filer": json.dumps(FILER), "file_row_number": number}
    row.update(changes)
    return row


FOOTER_ROWS = [(g, n, c, size) for g, n, sizes in (
    (0, 587, {}), (1, 447, {}), (2, 185, {"content": 106_112_978}), (3, 301, {}))
    for c, size in {**{k: 1000 for k in hf.CONTENT_COLUMNS}, **sizes}.items()]


class ManifestTests(unittest.TestCase):
    def test_revision_repo_and_paths(self):
        manifest = hf.load_manifest(manifest_bytes())
        self.assertEqual(hf.shards(manifest, "10-K"), [PATH])
        self.assertEqual(hf.resolve_url(manifest, PATH), hf.BASE_URL + PATH)
        for bad in (manifest_bytes(sha="0" * 40), manifest_bytes(id="other/repo"),
                    manifest_bytes(gated="auto"), manifest_bytes(paths=(PATH, PATH)),
                    manifest_bytes(paths=("10-K/../x.parquet",)), b"[1]", "str"):
            with self.assertRaises(hf.HfEdgarError):
                hf.load_manifest(bad)

    def test_url_only_from_manifest_paths(self):
        manifest = hf.load_manifest(manifest_bytes())
        for bad in ("10-K/other.parquet", "/etc/x.parquet", "10-K/a%2F.parquet",
                    "http://evil/x.parquet"):
            with self.assertRaises(hf.HfEdgarError):
                hf.resolve_url(manifest, bad)
        with self.assertRaises(hf.HfEdgarError):
            hf.shards(manifest, "10-K/../x")

    def test_saved_manifest(self):
        path = HF_DIR / "dataset-manifest.json"
        if not path.exists():
            self.skipTest("saved manifest absent")
        manifest = hf.load_manifest(path.read_bytes())
        self.assertEqual(len(hf.shards(manifest, "10-K")), 149)
        self.assertIn(PATH, manifest["paths"])


class IdentityTests(unittest.TestCase):
    def test_matching_row_keeps_raw_and_parsed(self):
        checked = hf.check_metadata(meta_row(), REFERENCE)
        self.assertEqual(checked["raw"]["metadata_filer"], json.dumps(FILER))
        self.assertEqual(checked["parsed"]["filer"], FILER)
        self.assertEqual(checked["parsed"]["documents"], {"present": False})

    def test_wrong_or_empty_identity_rejected(self):
        bad = {"accession": meta_row(**{"metadata_accession-number": "0001564590-22-026877"}),
               "date": meta_row(**{"metadata_filing-date": "20220729"}),
               "empty": meta_row(**{"metadata_filing-date": " "}),
               "cik": meta_row(metadata_filer=json.dumps(
                   {**FILER, "company-data": {"cik": "0000320193"}})),
               "form": meta_row(metadata_filer=json.dumps(
                   {**FILER, "filing-values": {"form-type": "10-K/A"}})),
               "double_encoded": meta_row(metadata_filer=json.dumps(json.dumps(FILER))),
               "no_filer": meta_row(metadata_filer=None)}
        for name, row in bad.items():
            with self.subTest(name), self.assertRaises(hf.HfEdgarError):
                hf.check_metadata(row, REFERENCE)

    def test_multi_filer_list_matches_reference_cik(self):
        other = {**FILER, "company-data": {"cik": "0000000001"}}
        checked = hf.check_metadata(meta_row(metadata_filer=json.dumps([other, FILER])),
                                    REFERENCE)
        self.assertEqual(checked["parsed"]["matched_filer_index"], 1)

    def test_viewer_preview_refused(self):
        for cells in ([], ["content"]):
            with self.assertRaisesRegex(hf.HfEdgarError, "viewer_preview"):
                hf.check_metadata(meta_row(truncated_cells=cells), REFERENCE)

    def test_duplicates_and_conflicts(self):
        with self.assertRaisesRegex(hf.HfEdgarError, "duplicate_rows"):
            hf.unique_match([meta_row(1), meta_row(2)], REFERENCE)
        with self.assertRaisesRegex(hf.HfEdgarError, "conflicting_rows"):
            hf.unique_match([meta_row(1), meta_row(2, metadata_period="x")], REFERENCE)
        with self.assertRaisesRegex(hf.HfEdgarError, "not_found"):
            hf.unique_match([], REFERENCE)

    def test_reference_row_must_be_unique(self):
        self.assertEqual(hf.reference_from_rows([REF_ROW], ACC, 789019), REFERENCE)
        for rows in ([], [REF_ROW, REF_ROW], [{**REF_ROW, "form": ""}]):
            with self.assertRaises(hf.HfEdgarError):
                hf.reference_from_rows(rows, ACC, 789019)

    def test_documents_described_not_interpreted(self):
        self.assertEqual(hf.describe_documents("not json")["json"], False)
        described = hf.describe_documents('[{"description": "10-K"}]')
        self.assertEqual((described["items"], described["keys"]), (1, ["description"]))


class PlanTests(unittest.TestCase):
    def test_negative_chunk_size_cannot_bypass_budget(self):
        for footer in ([(0, 10, "content", -1)], [(0, -10, "content", 1)],
                       [(False, 10, "content", 1)]):
            with self.assertRaisesRegex(hf.HfEdgarError, "footer_invalid"):
                hf.plan_row_group(footer, 0, ("content",), 100)

    def test_row_group_limit(self):
        with self.assertRaisesRegex(hf.HfEdgarError, "row_group_too_large"):
            hf.plan_row_group(FOOTER_ROWS, 1214, hf.CONTENT_COLUMNS, 100_000_000)
        plan = hf.plan_row_group(FOOTER_ROWS, 1214, hf.CONTENT_COLUMNS, 200_000_000)
        self.assertEqual((plan["row_group_id"], plan["row_group_first_row"]), (2, 1034))
        self.assertEqual(hf.plan_row_group(FOOTER_ROWS, 0, ("content",), 5000)["row_group_id"], 0)
        for row in (1520, -1):
            with self.assertRaises(hf.HfEdgarError):
                hf.plan_row_group(FOOTER_ROWS, row, ("content",), 10 ** 9)

    def test_saved_footer_probe(self):
        if not FOOTER.exists():
            self.skipTest("footer probe absent")
        footer = [tuple(r[:4]) for r in json.loads(FOOTER.read_bytes())["metadata"]]
        with self.assertRaisesRegex(hf.HfEdgarError, "row_group_too_large"):
            hf.plan_row_group(footer, 1214, hf.CONTENT_COLUMNS, 100_000_000)


class ExportTests(unittest.TestCase):
    def test_verbatim_no_overwrite(self):
        manifest = hf.load_manifest(manifest_bytes())
        origin = hf.source(manifest, PATH, 1214)
        content = "<html>\r\n<p>café &amp;  x</p>\n"
        with tempfile.TemporaryDirectory() as tmp:
            record = hf.export_content(tmp, origin, REFERENCE, meta_row(content=content), {})
            data = (Path(tmp) / f"{ACC}.content").read_bytes()
            self.assertEqual(data, content.encode("utf-8"))
            self.assertEqual(record["content"]["sha256"], hf.sha256(data))
            self.assertEqual((record["ready"], record["accepted_at"]), (False, None))
            with self.assertRaisesRegex(hf.HfEdgarError, "output_exists"):
                hf.export_content(tmp, origin, REFERENCE, meta_row(content=content), {})
            for row in (meta_row(content=""), meta_row(5, content="x")):
                with self.assertRaises(hf.HfEdgarError):
                    hf.export_content(Path(tmp) / "b", origin, REFERENCE, row, {})


class FakeReader:
    def __init__(self, fail_on=None, content="<html>x</html>"):
        self.calls, self.fail_on, self.content = [], fail_on, content

    def _log(self, name, url):
        self.calls.append((name, url))
        if self.fail_on and self.fail_on in url:
            raise OSError("HTTP 503")

    def accessions(self, url):
        self._log("accessions", url)
        return [("0000000000-00-000001", 0), (ACC, 1214)] if PATH in url else []

    def metadata(self, url, numbers):
        self._log("metadata", url)
        return [meta_row(n) for n in numbers]

    def footer(self, url):
        self._log("footer", url)
        return FOOTER_ROWS

    def full_row(self, url, number, accession):
        self._log("full_row", url)
        return [meta_row(number, content=self.content)]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.manifest = self.dir / "manifest.json"
        self.manifest.write_bytes(manifest_bytes(paths=(PATH, "10-K/b-2.parquet",
                                                        "10-K/c-3.parquet")))
        self.reference = self.dir / "MSFT.jsonl"
        self.reference.write_text(json.dumps(REF_ROW) + "\n")
        patcher = mock.patch.object(cli.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, reader, receipts, max_shards=2):
        return cli.main(["scan", "--manifest", str(self.manifest), "--form", "10-K",
                         "--reference", str(self.reference), "--reference-form", "10-K",
                         "--cutoff", "2024-10-04", "--receipts", str(receipts),
                         "--max-shards", str(max_shards)], reader_factory=lambda t, r: reader)

    def test_scan_caches_limits_and_stops_on_error(self):
        receipts = self.dir / "receipts"
        reader = FakeReader()
        self.assertEqual(self.scan(reader, receipts), 0)
        ok = json.loads((receipts / (Path(PATH).stem + ".json")).read_bytes())
        self.assertEqual([m["file_row_number"] for m in ok["matched"]], [1214])
        self.assertEqual(len({u for _, u in reader.calls}), 2)
        failing = FakeReader(fail_on="c-3")
        self.assertEqual(self.scan(failing, receipts, max_shards=3), 1)
        self.assertEqual({u for _, u in failing.calls}, {hf.BASE_URL + "10-K/c-3.parquet"})
        self.assertEqual(len(list(receipts.glob("failure-c-3-*.json"))), 1)
        self.assertFalse((receipts / "c-3.json").exists())

    def test_scan_refuses_foreign_dir_and_bad_limits(self):
        foreign = self.dir / "metadata-10k"
        foreign.mkdir()
        (foreign / "x.json").write_text('{"status": "ok"}')
        self.assertEqual(self.scan(FakeReader(), foreign), 1)
        self.assertEqual(self.scan(FakeReader(), self.dir / "r", max_shards=500), 1)

    def test_changed_reference_identity_does_not_reuse_cached_scan(self):
        receipts = self.dir / "receipts"
        self.assertEqual(self.scan(FakeReader(), receipts, max_shards=1), 0)
        self.reference.write_text(json.dumps({**REF_ROW, "cik": 320193}) + "\n")
        reader = FakeReader()
        self.assertEqual(self.scan(reader, receipts, max_shards=1), 1)
        self.assertEqual(reader.calls, [])

    def test_locate_and_extract(self):
        args = ["--manifest", str(self.manifest), "--path", PATH, "--reference",
                str(self.reference), "--accession", ACC, "--cik", "789019"]
        out = self.dir / "out"
        self.assertEqual(cli.main(["locate", *args, "--out", str(out)],
                                  reader_factory=lambda t, r: FakeReader()), 0)
        location = json.loads((out / f"location-{ACC}.json").read_bytes())
        self.assertIn("row_group_too_large", location["content_preflight"]["refused"])
        self.assertFalse(location["content_read"])
        reader = FakeReader()
        self.assertEqual(cli.main(["extract", *args, "--out", str(out), "--row-number",
                                   "1214"], reader_factory=lambda t, r: reader), 1)
        self.assertNotIn("full_row", [name for name, _ in reader.calls])
        self.assertEqual(len(list(out.glob("failure-extract-*.json"))), 1)
        self.assertEqual(cli.main(["extract", *args, "--out", str(out), "--row-number", "1214",
                                   "--max-row-group-bytes", "200000000"],
                                  reader_factory=lambda t, r: FakeReader()), 0)
        self.assertEqual((out / f"{ACC}.content").read_bytes(), b"<html>x</html>")

    def test_list_shards_offline(self):
        self.assertEqual(cli.main(["list-shards", "--manifest", str(self.manifest),
                                   "--form", "10-K"]), 0)


GPATH = "10-K/g-1.parquet"
GROUP_ACCS = [f"0001018724-2{i}-00000{i}" for i in range(5)]
GROUP_REFS = [{"accessionNumber": a, "cik": 1018724, "form": "10-K",
               "filingDate": f"202{i}-02-0{i + 1}", "primaryDocument": f"amzn-202{i}.htm"}
              for i, a in enumerate(GROUP_ACCS)]
GROUP_FOOTER = [(g, 100, c, 1000 if c != "content" else size)
                for g, size in ((0, 50_000), (1, 70_000)) for c in hf.CONTENT_COLUMNS]


def group_row(i, number=None, content=None, **changes):
    filer = {"company-data": {"cik": "0001018724"}, "filing-values": {"form-type": "10-K"}}
    body = content if content is not None else f"<html>AMZN {i}\r\n caf\u00e9</html>"
    docs = [{"filename": f"amzn-202{i}.htm", "secsgml_size_bytes": len(body.encode()),
             "type": "10-K", "sequence": "1", "description": "10-K"}]
    row = {"metadata_accession-number": GROUP_ACCS[i], "metadata_filing-date": f"202{i}020{i + 1}",
           "metadata_documents": json.dumps(docs), "metadata_period": None,
           "metadata_filer": json.dumps(filer), "file_row_number": 126 + i if number is None else number,
           "content": body}
    row.update(changes)
    return row


class GroupReader:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def metadata(self, url, numbers):
        self.calls.append(("metadata", tuple(numbers)))
        return [{k: v for k, v in r.items() if k != "content"} for r in self.rows
                if r["file_row_number"] in numbers]

    def footer(self, url):
        self.calls.append(("footer", None))
        return GROUP_FOOTER

    def full_rows(self, url, numbers):
        self.calls.append(("full_rows", tuple(numbers)))
        return [r for r in self.rows if r["file_row_number"] in numbers]


class GroupPlanTests(unittest.TestCase):
    def test_same_group_counted_once_groups_summed(self):
        plan = hf.plan_row_groups(GROUP_FOOTER, [10, 20, 30], hf.CONTENT_COLUMNS, 60_000, 60_000)
        self.assertEqual(plan["total_compressed_bytes"], 55_000)
        self.assertEqual([g["row_group_id"] for g in plan["row_groups"]], [0])
        plan = hf.plan_row_groups(GROUP_FOOTER, [10, 150], hf.CONTENT_COLUMNS, 80_000, 200_000)
        self.assertEqual(plan["total_compressed_bytes"], 55_000 + 75_000)
        with self.assertRaisesRegex(hf.HfEdgarError, "row_groups_too_large"):
            hf.plan_row_groups(GROUP_FOOTER, [10, 150], hf.CONTENT_COLUMNS, 80_000, 129_999)
        with self.assertRaisesRegex(hf.HfEdgarError, "row_group_too_large"):
            hf.plan_row_groups(GROUP_FOOTER, [150], hf.CONTENT_COLUMNS, 60_000, 10 ** 9)
        for rows in ([], [1, 1], [200]):
            with self.assertRaises(hf.HfEdgarError):
                hf.plan_row_groups(GROUP_FOOTER, rows, hf.CONTENT_COLUMNS, 10 ** 9, 10 ** 9)

    def test_targets_validated_and_duplicates_refused(self):
        targets = [{"accession": a, "cik": 1018724, "file_row_number": 126 + i}
                   for i, a in enumerate(GROUP_ACCS)]
        self.assertEqual(len(hf.group_targets(targets, GROUP_REFS)), 5)
        bad = {"dup_acc": targets + [{**targets[0], "file_row_number": 1}],
               "dup_row": targets[:2] + [{**targets[2], "file_row_number": 126}],
               "wrong_cik": [{**targets[0], "cik": 320193}],
               "extra_key": [{**targets[0], "form": "10-K"}],
               "too_many": targets * 5, "empty": []}
        for name, value in bad.items():
            with self.subTest(name), self.assertRaises(hf.HfEdgarError):
                hf.group_targets(value, GROUP_REFS)

    def test_primary_document_check(self):
        docs = hf.describe_documents(json.dumps([{"filename": "a.htm", "secsgml_size_bytes": 3}]))
        self.assertEqual(hf.check_primary_document(docs, "a.htm", b"abc")["status"],
                         "consistent_within_dataset")
        for name, data in (("b.htm", b"abc"), ("a.htm", b"abcd")):
            with self.assertRaises(hf.HfEdgarError):
                hf.check_primary_document(docs, name, data)
        self.assertEqual(hf.check_primary_document({"present": False}, "a.htm", b"")["status"],
                         "not_comparable")


class GroupCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.manifest = self.dir / "manifest.json"
        self.manifest.write_bytes(manifest_bytes(paths=(PATH, GPATH)))
        self.reference = self.dir / "all.jsonl"
        self.reference.write_text("".join(json.dumps(r) + "\n" for r in GROUP_REFS))
        self.targets = [{"accession": a, "cik": 1018724, "file_row_number": 126 + i}
                        for i, a in enumerate(GROUP_ACCS)]

    def tearDown(self):
        self.tmp.cleanup()

    def run_group(self, reader, out, targets=None, total="200000"):
        path = self.dir / f"targets-{len(list(self.dir.glob('targets-*')))}.json"
        path.write_text(json.dumps(self.targets if targets is None else targets))
        return cli.main(["extract-group", "--manifest", str(self.manifest), "--path", GPATH,
                         "--reference", str(self.reference), "--targets", str(path),
                         "--out", str(out), "--max-total-bytes", total],
                        reader_factory=lambda t, r: reader)

    def test_five_rows_one_group_one_content_query_verbatim(self):
        rows = [group_row(i) for i in range(5)]
        reader, out = GroupReader(rows), self.dir / "out"
        self.assertEqual(self.run_group(reader, out), 0)
        self.assertEqual([c[0] for c in reader.calls], ["metadata", "footer", "full_rows"])
        for i, row in enumerate(rows):
            data = (out / f"{GROUP_ACCS[i]}.content").read_bytes()
            self.assertEqual(data, row["content"].encode("utf-8"))
            record = json.loads((out / f"{GROUP_ACCS[i]}.content.provenance.json").read_bytes())
            self.assertEqual(record["metadata"]["raw"]["metadata_filer"], row["metadata_filer"])
            self.assertEqual(record["primary_document_check"]["status"], "consistent_within_dataset")
            self.assertEqual(record["row_group_plan"]["row_group_id"], 1)
        receipt = json.loads(next(out.glob("group-*.json")).read_bytes())
        self.assertEqual((receipt["status"], receipt["content_queries"]), ("ok", 1))
        self.assertEqual(receipt["plan"]["total_compressed_bytes"], 75_000)
        # second run refuses to overwrite before any network read
        again = GroupReader(rows)
        self.assertEqual(self.run_group(again, out), 1)
        self.assertEqual(again.calls, [])

    def test_failures_write_nothing(self):
        cases = {
            "missing_row": ([group_row(i) for i in range(4)], None, "metadata"),
            "wrong_cik": ([group_row(i) for i in range(4)] + [group_row(4, metadata_filer=json.dumps(
                {"company-data": {"cik": "0000320193"}, "filing-values": {"form-type": "10-K"}}))],
                None, "metadata"),
            "wrong_form": ([group_row(i) for i in range(4)] + [group_row(4, metadata_filer=json.dumps(
                {"company-data": {"cik": "0001018724"}, "filing-values": {"form-type": "10-K/A"}}))],
                None, "metadata"),
            "bad_date": ([group_row(i) for i in range(4)] + [group_row(
                4, **{"metadata_filing-date": "20240206"})], None, "metadata"),
            "size_mismatch": ([group_row(i) for i in range(4)] + [group_row(
                4, metadata_documents=json.dumps([{"filename": "amzn-2024.htm",
                                                   "secsgml_size_bytes": 1}]))], None, "full_rows"),
            "over_budget": ([group_row(i) for i in range(5)], "74999", "footer"),
        }
        for name, (rows, total, last) in cases.items():
            with self.subTest(name):
                reader, out = GroupReader(rows), self.dir / name
                self.assertEqual(self.run_group(reader, out, total=total or "200000"), 1)
                self.assertEqual(reader.calls[-1][0], last)
                self.assertEqual(list(out.glob("*.content")), [])
                self.assertEqual(list(out.glob("group-*.json")), [])
                self.assertEqual(len(list(out.glob("failure-group-*.json"))), 1)
                failure = json.loads(next(out.glob("failure-group-*.json")).read_bytes())
                self.assertFalse(failure["content_written"])
                self.assertEqual(failure["content_files_present"], [])

    def test_duplicate_targets_refused_offline(self):
        reader = GroupReader([group_row(i) for i in range(5)])
        dup = self.targets + [dict(self.targets[0])]
        self.assertEqual(self.run_group(reader, self.dir / "d", targets=dup), 1)
        self.assertEqual(reader.calls, [])


if __name__ == "__main__":
    unittest.main()
