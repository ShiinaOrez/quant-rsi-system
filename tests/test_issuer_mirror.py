"""Tests for scripts/issuer_mirror.py (pager traversal, duplicates, form mismatch, fetch/coverage reporting).

Synthetic pages follow the markup of the saved intc.com pages; the saved pages themselves are
checked too when present. Runs under pytest or `python -m unittest tests.test_issuer_mirror`.
"""
import importlib.util
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("issuer_mirror", ROOT / "scripts" / "issuer_mirror.py")
mirror = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mirror)
SAVED = ROOT / "artifacts" / "benchmark-quality" / "issuer-mirrors"
LANDING = "https://www.intc.com/filings-reports/all-sec-filings"
FORMS = ["10-K", "10-K/A", "10-Q", "8-K", "8-K/A", "4"]
YEARS = list(range(2026, 1993, -1))


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status, self._body, self.headers = status, body, headers or {"Content-Type": "text/html"}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Clock:
    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def row(date, form, acc, main="main.htm", exhibits=(), xbrl=True):
    base = f"/filings-reports/all-sec-filings/content/{acc}"
    exp = "".join(f'<a href="{base}/{e}" class="fancybox" data-fancybox="#document-1-{acc}">{label}</a><br>'
                  for label, e in exhibits)
    docs = (f'<div class="spr-expander"><strong>Documents</strong><div class="spr-expandable">{exp}</div></div>'
            if exhibits else "")
    zip_link = (f'<a href="https://www.intc.com{base}/{acc}-xbrl.zip" title="Download Raw XBRL Files">'
                f'<img src="x.png" alt="XAML" /></a>' if xbrl else "")
    return (f'<tr><td class="spr-sec-date">{date}</td>'
            f'<td class="spr-sec-form"><a href="{base}/{main}" data-fancybox="#document-1-{acc}">{form}</a></td>'
            f'<td class="spr-sec-desc"><a href="{base}/{main}" class="fancybox doc-title">Report</a>{docs}</td>'
            f'<td class="spr-sec-view-pdf"><a href="https://www.intc.com{base}/{acc}.pdf" title="PDF"></a></td>'
            f'<td class="spr-sec-view-docs">{zip_link}</td><td class="spr-sec-pages">10</td></tr>')


def page(rows, form=None, active=1, last=1, next_page=True, next_href=None):
    opts = "".join(f'<option value="{f}">{f}</option>' for f in FORMS)
    years = "".join(f"<option >{y}</option>" for y in YEARS)
    query = f"form_type={urllib.parse.quote(form)}&year=&" if form else ""
    pager = ""
    if last > 1:
        items = "".join(f'<li class="{"active" if n == active else ""}"><a href="{LANDING}?{query}page={n}">{n}</a></li>'
                        for n in range(1, last + 1))
        if active < last and next_page:
            href = next_href or f"{LANDING}?{query}page={active + 1}"
            items += f'<li><a href="{href}"><span class="glyphicon"><span class="sr-only">Next</span></span></a></li>'
        pager = f'<ul class="pagination">{items}</ul>'
    return (f'<html><head><base href="https://www.intc.com" /></head><body>'
            f'<form action="{LANDING}" id="sec-filing-filter"><select name="form_type" id="form_type">'
            f'<option value="">View All</option>{opts}</select><select name="year" id="year">'
            f'<option value="">View All</option>{years}</select></form>'
            f'<table class="content-table spr-ir-sec-filings"><thead><tr><th class="spr-sec-date">Date</th></tr>'
            f'</thead><tbody>{"".join(rows)}</tbody></table>'
            f'<div class="pagination-wrapper">{pager}</div></body></html>').encode()


class Site:
    """Routes URLs to (status, body); records every request with the fake clock time."""

    def __init__(self, routes, clock):
        self.routes, self.clock, self.calls = routes, clock, []

    def __call__(self, req, timeout):
        self.calls.append((req.full_url, self.clock(), timeout))
        status, body = self.routes.get(req.full_url, (404, b"not found"))
        if status != 200:
            import urllib.error
            import io
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(body))
        ctype = "application/zip" if req.full_url.endswith(".zip") else "text/html"
        return FakeResponse(200, body, {"Content-Type": ctype})


def form_url(form, page_no=None):
    q = urllib.parse.urlencode({"form_type": form, "year": ""})
    return f"{LANDING}?{q}" + (f"&page={page_no}" if page_no else "")


def page_url(form, n):
    return form_url(form) if n == 1 else f"{LANDING}?form_type={urllib.parse.quote(form)}&year=&page={n}"


def make(routes, tmp):
    clock = Clock()
    site = Site(routes, clock)
    store = mirror.arch.Store(tmp)
    f = mirror.make_fetcher(store, opener=site, sleep=clock.sleep, clock=clock)
    return f, site, clock


def ten_q_rows(start, n, prefix="0000050863-24-"):
    return [row(f"0{1 + (i % 9)}/15/24", "10-Q", f"{prefix}{start + i:06d}") for i in range(n)]


def base_routes():
    """Landing + single empty page for every plan form except 10-Q (tests add those)."""
    routes = {LANDING: (200, page([], last=398))}
    for f in ("10-K", "10-K/A", "8-K", "8-K/A"):
        routes[form_url(f)] = (200, page([], form=f))
    return routes


class SavedPageTests(unittest.TestCase):
    @unittest.skipUnless((SAVED / "INTC-2024-10Q-index.html").exists(), "saved mirror pages not present")
    def test_saved_pages_parse_to_known_links(self):
        landing = mirror.parse_index_page((SAVED / "INTC.html").read_bytes(), LANDING)
        self.assertEqual(landing["pager"]["last_page"], 398)
        self.assertEqual(landing["pager"]["next_url"], LANDING + "?page=2")
        self.assertNotIn("10-Q/A", landing["options"]["form_type"])
        years = sorted(int(y) for y in landing["options"]["year"] if y)
        q = mirror.parse_index_page((SAVED / "INTC-2024-10Q-index.html").read_bytes(), LANDING)
        recs = [mirror.normalize_row(r, q["base"], years, "10-Q") for r in q["rows"]]
        self.assertEqual([r[1] for r in recs], [[], [], []])
        known = json.loads((SAVED / "INTC-2024-10Q-links.json").read_text())
        for (rec, _), k in zip(recs, known):
            files = {f["role"]: f["url"] for f in rec["files"] if f["role"] != "attachment"}
            self.assertEqual(files, {"main": k["document"], "xbrl_zip": k["xbrl"]})
            self.assertIsNone(rec["accepted_at"])
        self.assertEqual(recs[0][0]["filing_date"], "2024-11-01")
        self.assertEqual(sum(f["role"] == "attachment" for f in recs[0][0]["files"]), 8)


class ParseTests(unittest.TestCase):
    def test_two_digit_year_resolved_only_via_offered_years(self):
        self.assertEqual(mirror.resolve_date("02/10/94", YEARS), "1994-02-10")
        self.assertEqual(mirror.resolve_date("02/10/24", YEARS), "2024-02-10")
        self.assertIsNone(mirror.resolve_date("02/10/80", YEARS))
        self.assertIsNone(mirror.resolve_date("2024-02-10", YEARS))

    def test_row_with_mixed_accessions_is_flagged(self):
        html = row("01/02/24", "8-K", "0000050863-24-000001",
                   exhibits=[("EX-99.1", "../0000050863-24-000002/ex991.htm")])
        p = mirror.parse_index_page(page([html]), LANDING)
        _, findings = mirror.normalize_row(p["rows"][0], p["base"], YEARS, "8-K")
        self.assertIn("row_accession_missing_or_mixed", [f["check"] for f in findings])


class PlanTests(unittest.TestCase):
    def test_full_traversal_follows_next_links_and_is_complete(self):
        routes = base_routes()
        routes[page_url("10-Q", 1)] = (200, page(ten_q_rows(1, 2), "10-Q", 1, 3))
        routes[page_url("10-Q", 2)] = (200, page(ten_q_rows(3, 2), "10-Q", 2, 3))
        routes[page_url("10-Q", 3)] = (200, page(ten_q_rows(5, 1), "10-Q", 3, 3))
        with tempfile.TemporaryDirectory() as tmp:
            f, site, clock = make(routes, tmp)
            summary = mirror.cmd_plan(f, mirror.MIRRORS["INTC"])
            tq = next(t for t in summary["traversals"] if t["form"] == "10-Q")
            self.assertEqual((tq["last_page"], tq["rows"], tq["truncated"], tq["pending_pages"]), (3, 5, False, []))
            self.assertEqual(summary["forms_not_offered"], ["10-Q/A"])
            self.assertEqual(summary["findings"], 0)
            # one global slot: never two requests within one second, across hosts and forms
            times = [t for _, t, _ in site.calls]
            self.assertTrue(all(b - a >= 1.0 for a, b in zip(times, times[1:])))
            self.assertTrue(all(timeout == 20.0 for *_, timeout in site.calls))
            cov = mirror.cmd_coverage(f.store, mirror.MIRRORS["INTC"])
            self.assertEqual({x["form"]: x["pager_complete"] for x in cov["forms"]}["10-Q"], True)
            self.assertIn("form_filter_not_offered_by_mirror:10-Q/A", cov["gaps"])
            self.assertIn("accepted_at_unavailable_from_mirror:all_filings_null", cov["gaps"])
            self.assertFalse(cov["benchmark_ready"])
            # rerun uses the page cache (resumable) without new requests
            n = len(site.calls)
            mirror.cmd_plan(f, mirror.MIRRORS["INTC"])
            self.assertEqual(len(site.calls), n)

    def test_max_pages_reports_truncation_and_pending_pages(self):
        routes = base_routes()
        routes[page_url("10-Q", 1)] = (200, page(ten_q_rows(1, 2), "10-Q", 1, 3))
        with tempfile.TemporaryDirectory() as tmp:
            f, _, _ = make(routes, tmp)
            summary = mirror.cmd_plan(f, mirror.MIRRORS["INTC"], max_pages=1)
            tq = next(t for t in summary["traversals"] if t["form"] == "10-Q")
            self.assertEqual((tq["truncated"], tq["pending_pages"], tq["stop_reason"]), (True, [2, 3], "max_pages=1"))
            cov = mirror.cmd_coverage(f.store, mirror.MIRRORS["INTC"])
            self.assertFalse(next(x for x in cov["forms"] if x["form"] == "10-Q")["pager_complete"])
            self.assertIn("pager_incomplete:10-Q:pending_pages=2", cov["gaps"])

    def test_failed_page_and_unexpected_next_link_stop_traversal(self):
        routes = base_routes()
        routes[page_url("10-Q", 1)] = (200, page(ten_q_rows(1, 2), "10-Q", 1, 3))
        routes[page_url("10-Q", 2)] = (500, b"oops")
        bad = base_routes()
        bad[page_url("10-Q", 1)] = (200, page(ten_q_rows(1, 2), "10-Q", 1, 3, next_href=LANDING + "?page=2"))
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as tmp2:
            f, _, _ = make(routes, tmp)
            tq = next(t for t in mirror.cmd_plan(f, mirror.MIRRORS["INTC"])["traversals"] if t["form"] == "10-Q")
            self.assertEqual((tq["truncated"], tq["pending_pages"], tq["stop_reason"]), (True, [2, 3], "http_500:None"))
            f2, _, _ = make(bad, tmp2)
            tq = next(t for t in mirror.cmd_plan(f2, mirror.MIRRORS["INTC"])["traversals"] if t["form"] == "10-Q")
            self.assertEqual(tq["stop_reason"], "next_link_unexpected")
            self.assertEqual(tq["pending_pages"], [2, 3])

    def test_duplicates_and_conflicts_and_form_mismatch(self):
        routes = base_routes()
        dup = row("02/15/24", "10-Q", "0000050863-24-000002")
        conflict = row("02/15/24", "10-Q", "0000050863-24-000003")
        routes[page_url("10-Q", 1)] = (200, page(ten_q_rows(1, 3), "10-Q", 1, 2))
        routes[page_url("10-Q", 2)] = (200, page([dup, conflict, row("03/01/24", "10-Q/A", "0000050863-24-000009")],
                                                 "10-Q", 2, 2))
        # The 8-K filter returns an 8-K/A row; the 8-K/A filter lists the same filing.
        amend = row("04/01/24", "8-K/A", "0000050863-24-000020")
        routes[form_url("8-K")] = (200, page([amend], "8-K"))
        routes[form_url("8-K/A")] = (200, page([amend], "8-K/A"))
        with tempfile.TemporaryDirectory() as tmp:
            f, _, _ = make(routes, tmp)
            checks = mirror.cmd_plan(f, mirror.MIRRORS["INTC"])["findings_by_check"]
            self.assertEqual(checks["duplicate_across_pages_pagination_shift"], 1)
            self.assertEqual(checks["duplicate_conflict"], 1)
            self.assertEqual(checks["form_mismatch_vs_request"], 2)  # 10-Q/A under 10-Q, 8-K/A under 8-K
            self.assertEqual(checks["duplicate_across_form_filters"], 1)
            index = json.loads((Path(tmp) / "mirror/INTC/index.json").read_text())
            accs = [x["accession"] for x in index["filings"]]
            self.assertEqual(len(accs), len(set(accs)))
            self.assertIn("0000050863-24-000009", accs)  # 10-Q/A kept although the mirror has no 10-Q/A filter
            cov = mirror.cmd_coverage(f.store, mirror.MIRRORS["INTC"])
            self.assertFalse(next(x for x in cov["forms"] if x["form"] == "10-Q")["pager_complete"])


class FetchTests(unittest.TestCase):
    def test_truncated_html_and_negative_zip_offset_are_reported(self):
        item = {"filename": "report.htm"}
        self.assertEqual(mirror.check_payload(item, b"<html><body>cut off</bo", "text/html")[0],
                         ["html_end_tag_missing_possible_truncation"])
        self.assertEqual(mirror.check_payload(item, b"<html>complete</html>", "text/html")[0], [])
        import io
        import struct
        import zipfile
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("report.xml", b"<xml/>")
        data = bytearray(buffer.getvalue())
        end = data.rfind(b"PK\x05\x06")
        offset = struct.unpack_from("<I", data, end + 16)[0]
        struct.pack_into("<I", data, end + 16, offset + 45)
        self.assertIn("zip_invalid_member_offset",
                      mirror.check_payload({"filename": "filing.zip"}, bytes(data), "application/zip")[0])

    def setUp(self):
        self.routes = base_routes()
        acc, base = "0000050863-24-000124", "https://www.intc.com/filings-reports/all-sec-filings/content/"
        rows = [row("08/02/24", "10-Q", acc, exhibits=[("EX-31.1", "ex311.htm"), ("EX-32.1", "ex321.htm"),
                                                       ("GRAPHIC", "g1.jpg")]),
                row("11/01/24", "10-Q", "0000050863-24-000149")]
        self.routes[page_url("10-Q", 1)] = (200, page(rows, "10-Q"))
        self.routes[base + acc + "/main.htm"] = (200, b"<html>10-Q body</html>")
        self.routes[base + acc + "/ex311.htm"] = (200, b"<html>cert</html>")
        self.routes[base + acc + f"/{acc}-xbrl.zip"] = (200, b"<html>not a zip</html>")
        self.routes[base + acc + "/g1.jpg"] = (200, b"\xff\xd8 image")
        # ex321.htm has no route -> 404

    def test_fetch_limit_resume_failures_and_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            f, site, _ = make(self.routes, tmp)
            mirror.cmd_plan(f, mirror.MIRRORS["INTC"])
            queue = mirror.load_queue(f.store, "INTC")
            self.assertEqual({i["accession"] for i in queue}, {"0000050863-24-000124"})  # 11/01/24 beyond horizon
            self.assertTrue(all(i["accepted_at"] is None and i["filing_date"] == "2024-08-02" for i in queue))
            r0 = mirror.cmd_fetch(f, "INTC", limit=10, roles=["xbrl_zip"], accessions=["0000050863-24-000124"])
            self.assertEqual([i["filename"] for i in r0["items"]], ["0000050863-24-000124-xbrl.zip"])
            r1 = mirror.cmd_fetch(f, "INTC", limit=2)
            self.assertEqual((r1["fetched"], r1["processed"]), (2, 2))
            r2 = mirror.cmd_fetch(f, "INTC", limit=10)
            self.assertEqual((r2["skipped_cached"], r2["fetched"], r2["failed"], r2["skipped_images"]), (3, 0, 1, 1))
            n = len(site.calls)
            r3 = mirror.cmd_fetch(f, "INTC", limit=10)
            self.assertEqual((r3["processed"], r3["skipped_failed"], len(site.calls)), (0, 1, n))
            cov = mirror.cmd_coverage(f.store, mirror.MIRRORS["INTC"])
            files = cov["files"]
            self.assertEqual((files["queued"], files["fetched"], files["pending"]), (5, 3, 1))
            self.assertEqual([(x["filename"], x["status"]) for x in files["failed"]], [("ex321.htm", 404)])
            self.assertEqual(files["pending_images_skipped_by_default"], 1)
            self.assertEqual(files["payload_problems"],
                             [{"accession": "0000050863-24-000124", "filename": "0000050863-24-000124-xbrl.zip",
                               "problems": ["zip_magic_missing"]}])
            self.assertEqual(cov["filings"]["in_horizon_all_files_fetched"], 0)
            self.assertEqual(cov["filings"]["beyond_horizon_not_queued"], 1)
            item = files["fetched_items"][0]
            for k in ("sha256", "retrieved_at", "url", "filing_date", "accession"):
                self.assertTrue(item[k])
            self.assertIsNone(item["accepted_at"])
            self.assertFalse(cov["pit_ready"])

    def test_403_trips_breaker_and_stops_fetch(self):
        acc = "0000050863-24-000124"
        base = "https://www.intc.com/filings-reports/all-sec-filings/content/" + acc
        with tempfile.TemporaryDirectory() as tmp:
            f, site, _ = make(self.routes, tmp)
            mirror.cmd_plan(f, mirror.MIRRORS["INTC"])
            self.routes[base + "/main.htm"] = (403, b"Forbidden")
            res = mirror.cmd_fetch(f, "INTC", limit=10)
            self.assertEqual((res["failed"], res["blocked"], res["processed"]), (1, True, 2))
            self.assertEqual(sum(1 for u, *_ in site.calls if "/content/" in u), 1)
            cov = mirror.cmd_coverage(f.store, mirror.MIRRORS["INTC"])
            self.assertEqual([(x["filename"], x["status"]) for x in cov["files"]["failed"]], [("main.htm", 403)])
            self.assertEqual(cov["files"]["pending_blocked_not_attempted"], 1)

    def test_index_page_served_for_document_is_a_payload_problem(self):
        item = {"filename": "doc.htm"}
        self.assertEqual(mirror.check_payload(item, page([]), "text/html")[0],
                         ["index_page_returned_instead_of_document"])
        self.assertEqual(mirror.check_payload(item, b"", "text/html")[0], ["empty_body"])


if __name__ == "__main__":
    unittest.main()
