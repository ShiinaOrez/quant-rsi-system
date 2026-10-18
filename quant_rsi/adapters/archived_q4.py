"""Explicit transport plans for hash-pinned archived issuer feeds.

The observed URLs stay unchanged. HTTPS candidates are separate, restricted by
issuer/CIK/CDN/path, and never imply original filing bytes or publication time.
The regular Q4 planner still refuses HTTP URLs by default.
"""
import base64
import hashlib
import json
import re
from collections import Counter
from datetime import date, datetime
from urllib.parse import urlsplit, urlunsplit

from quant_rsi.adapters import q4_filings as q4


def plan_capture(raw, policy, *, through):
    if policy.get("schema") != "archived-q4-https.v1":
        raise q4.FeedError("unsupported_archived_transport_policy")
    symbol = policy.get("issuer")
    info = q4.issuer(symbol)
    if (policy.get("cik") != info["cik"] or policy.get("source_origin") != info["origin"]
            or policy.get("source_path") != "/feed/SECFiling.svc/GetEdgarFilingList"
            or not re.fullmatch(r"[0-9]{14}", str(policy.get("capture_timestamp", "")))):
        raise q4.FeedError("capture_identity_mismatch")
    if (policy.get("transport_operation") != "http_to_https_same_host_and_path"
            or policy.get("sec_original_bytes_verified") is not False
            or policy.get("publication_time_verified") is not False):
        raise q4.FeedError("transport_policy_scope_mismatch")
    try:
        datetime.strptime(policy["capture_timestamp"], "%Y%m%d%H%M%S")
        date.fromisoformat(through)
    except (ValueError, TypeError) as err:
        raise q4.FeedError("capture_timestamp_or_cutoff_invalid") from err
    digest = hashlib.sha256(raw).hexdigest()
    sha1 = base64.b32encode(hashlib.sha1(raw).digest()).decode("ascii")
    if (type(policy.get("payload_bytes")) is not int or len(raw) != policy["payload_bytes"]
            or digest != policy.get("payload_sha256") or sha1 != policy.get("cdx_digest_sha1_base32")):
        raise q4.FeedError("capture_hash_or_size_mismatch")
    plan = q4.plan_feed(json.loads(raw), symbol, source_sha256=digest, through=through)
    plan["observed_stats"] = plan["stats"].copy()
    transformations = Counter()
    for row in plan["rows"]:
        for doc in row["documents"]:
            observed = doc["url"]
            candidate, operation = observed, "none"
            if doc["url_issue"] == "http_not_upgraded":
                parts = urlsplit(observed)
                candidate = urlunsplit(("https", parts.netloc, parts.path, parts.query, parts.fragment))
                extensions = q4.DOCUMENT_ROLES.get(doc["document_type"], (None, None, ()))[2]
                ok, reason = q4.check_cdn_url(candidate, symbol, extensions)
                if not ok:
                    doc["transport_issue"] = reason
                    candidate = None
                else:
                    operation = policy["transport_operation"]
                    doc["observed_url_issue"] = doc["url_issue"]
                    doc["fetchable"], doc["url_issue"] = True, None
            if not doc["fetchable"]:
                candidate = None
            doc["fetch_url"] = candidate
            doc["transport_operation"] = operation
            if candidate:
                transformations[f"{doc['document_type']}:{operation}"] += 1
    stats = {k: v for k, v in plan["stats"].items() if not k.startswith("doc:")}
    for row in plan["rows"]:
        for doc in row["documents"]:
            key = f"doc:{doc['document_type']}:{'fetchable' if doc['fetchable'] else doc['url_issue']}"
            stats[key] = stats.get(key, 0) + 1
    plan["stats"] = stats
    plan["transport_counts"] = dict(transformations)
    plan["source"] = dict(policy, role="third_party_archive_of_issuer_feed",
                          capture_payload_hash_verified=True, available_at=None, ready=False)
    return plan
