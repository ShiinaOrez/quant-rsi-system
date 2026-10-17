"""Conservative extraction of traditional XBRL 2.1 instance documents from SEC filing ZIPs.

Companion to inline_xbrl.py and reusing its ZIP/XML safety, context/unit parsing, duplicate
grouping, role binding and provenance. Each item fact keeps its raw text, QName, resolved context
and unit, id, precision/decimals, nil, inherited xml:lang, footnotes and source location
(member, line, column, element path). Rules (XBRL 2.1 REC 2003-12-31 + errata 2013-02-20):

* an item has @contextRef; numeric items carry @unitRef and non-numeric items must not (4.6.2).
  That structural rule is the only basis for kind; the taxonomy item type (monetary, shares,
  date, ...) is never guessed from the unit, and item_type stays null because linked taxonomy
  schemas are not fetched;
* the element text is the complete value. decimals/precision describe accuracy only (4.6.4 /
  4.6.5) and nothing is scaled. Numbers must match the xs:decimal lexical space (XML Schema
  Part 2, 3.2.3; whitespace collapse), so a leading "-" is a negative value and "+" is allowed;
* xs:float/xs:double forms (exponent, INF, NaN) are only valid if the concept's type derives
  from float/double, which cannot be verified offline: such facts stay raw_only with a note;
* value is the canonical exact decimal ("12.50" -> "12.5"); raw_text keeps the lexical form;
* a non-nil numeric item needs exactly one of decimals/precision; a nil one has neither (4.6.3);
* tuples, fractions and items with element content are reported as unsupported, never flattened;
* several instance documents in one ZIP are all extracted and flagged, never silently reduced
  to one; a ZIP that mixes inline and instance documents is refused.

Without a caller-supplied, timezone-aware available_at every fact has available_at=None and
pit_ready=False, and the result is never task ready.
"""
import re
from decimal import Decimal, InvalidOperation
from xml.parsers import expat

from quant_rsi.adapters import inline_xbrl as ix
from quant_rsi.adapters.inline_xbrl import ExtractionError, Limits, LINK, XBRLI, XLINK, XML_NS, XSI

SCHEMA = "xbrl-instance-facts.v1"
NUMERIC, NON_NUMERIC = "numericItem", "nonNumericItem"
_DECIMAL = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)")
_FLOAT_ONLY = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)[eE][+-]?[0-9]+|[+-]?INF|NaN")


class _Stop(Exception):
    pass


def root_tag(data):
    """Clark name of the document element, reading no further; DTDs are refused."""
    parser = expat.ParserCreate(namespace_separator="}")
    found = []

    def start(name, _attrs):
        found.append(ix._clark(name))
        raise _Stop

    def refuse(*_):
        raise ExtractionError("doctype_refused")

    parser.StartElementHandler = start
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    try:
        parser.Parse(data, True)
    except _Stop:
        return found[0]
    except expat.ExpatError:
        return None
    return None


def classify_members(members):
    """Inline documents (.htm/.html/.xhtml with the iXBRL 1.1 namespace) and instance documents
    (.xml whose root element is xbrli:xbrl)."""
    inline, instance = [], []
    for name, data in sorted(members.items()):
        lower = name.lower()
        if lower.endswith((".htm", ".html", ".xhtml")) and ix.IX.encode() in data:
            inline.append(name)
        elif lower.endswith(".xml") and root_tag(data) == f"{{{XBRLI}}}xbrl":
            instance.append(name)
    return inline, instance


def _lang(node):
    while node is not None:
        if f"{{{XML_NS}}}lang" in node.attrib:
            return node.attrib[f"{{{XML_NS}}}lang"]
        node = node.parent
    return None


def _nil(node, errors):
    value = node.attrib.get(f"{{{XSI}}}nil")
    if value is None:
        return False
    value = ix._collapse(value)
    if value not in ("true", "false", "0", "1"):
        errors.append("invalid_xsi_nil")
    return value in ("true", "1")


def _numeric(node, doc, errors, notes):
    a = node.attrib
    nil = _nil(node, errors)
    raw = node.text()
    decimals, precision = a.get("decimals"), a.get("precision")
    if nil:
        if raw.strip():
            errors.append("nil_with_content")
        if decimals is not None or precision is not None:
            errors.append("nil_with_decimals_or_precision")
    else:
        if decimals is not None and precision is not None:
            errors.append("both_decimals_and_precision")
        elif decimals is None and precision is None:
            errors.append("decimals_or_precision_required")
        if decimals is not None and decimals != "INF" and ix._int_attr(decimals) is None:
            errors.append("invalid_decimals")
        if precision is not None and precision != "INF" and ix._int_attr(precision, r"[0-9]+") is None:
            errors.append("invalid_precision")
    if node.has_comment:
        errors.append("comment_inside_value")
    value = None
    text = ix._collapse(raw)
    if not nil and not errors:
        if _DECIMAL.fullmatch(text):
            digits = text.lstrip("+-").replace(".", "").lstrip("0")
            if len(digits) > doc["limits"].max_digits:
                errors.append("too_many_digits")
            else:
                try:
                    value = Decimal(text)
                    # Same number, one spelling: "0.0490" and "0.049" are one xs:decimal value
                    # (XML Schema 3.2.3); raw_text keeps the lexical form.
                    if value == 0:
                        value = Decimal(0)
                    elif value.as_tuple().exponent < 0:
                        value = Decimal(format(value, "f").rstrip("0").rstrip("."))
                except InvalidOperation:
                    errors.append("decimal_error")
        elif _FLOAT_ONLY.fullmatch(text):
            notes.append("float_lexical_requires_float_or_double_type_unverified")
        elif not text:
            errors.append("empty_value")
        else:
            errors.append("invalid_numeric_lexical")
    return {"raw_text": raw, "nil": nil, "decimals": decimals, "precision": precision,
            "value": ix.plain(value)}


def _nonnumeric(node, errors):
    a = node.attrib
    nil = _nil(node, errors)
    raw = node.text()
    if nil and raw.strip():
        errors.append("nil_with_content")
    if a.get("decimals") is not None or a.get("precision") is not None:
        errors.append("decimals_or_precision_on_non_numeric")
    # The concept's type is unknown, so the text is not normalised or converted.
    return {"raw_text": raw, "nil": nil, "decimals": None, "precision": None,
            "text_sha256": ix._sha256(raw.encode("utf-8")), "value": None if (nil or errors) else raw}


def _fact(node, doc):
    errors, notes = [], []
    a = node.attrib
    uri, _, local = node.tag[1:].partition("}") if node.tag.startswith("{") else ("", "", node.tag)
    prefix = next((p for p, u in node.ns.items() if u == uri and p and doc["root_ns"].get(p) == uri), None)
    concept = {"qname": f"{prefix}:{local}" if prefix else local, "namespace": uri or None, "local_name": local}
    if not uri:
        errors.append("concept_without_namespace")
    elif prefix is None:
        notes.append("concept_prefix_not_bound_on_root")
    if node.children:
        errors.append("unsupported_fraction" if any(c.tag.startswith(f"{{{XBRLI}}}") for c in node.children)
                      else "item_with_element_content")
    ctx_ref, unit_ref = a.get("contextRef"), a.get("unitRef")
    context = doc["contexts"].get(ctx_ref)
    if context is None:
        errors.append("missing_context")
    elif ctx_ref in doc["duplicate_ids"]:
        errors.append("ambiguous_context_id")
    elif context["errors"]:
        errors.append("invalid_context")
    unit = None
    if unit_ref is not None:
        kind = NUMERIC
        unit = doc["units"].get(unit_ref)
        if unit is None:
            errors.append("missing_unit")
        elif unit_ref in doc["duplicate_ids"]:
            errors.append("ambiguous_unit_id")
        elif unit["errors"]:
            errors.append("invalid_unit")
        body = _numeric(node, doc, errors, notes)
    else:
        kind = NON_NUMERIC
        body = _nonnumeric(node, errors)
    status = "error" if errors else "nil" if body["nil"] else "ok" if body["value"] is not None else "raw_only"
    return {"fact_id": a.get("id"), "kind": kind, "kind_basis": "xbrl21_4.6.2_unitRef_presence",
            "item_type": None, "concept": concept, **body,
            # Inline-only properties; an instance value is never scaled or sign-flipped.
            "format": None, "scale": None, "sign": None,
            "status": status, "errors": errors, "notes": notes, "context_ref": ctx_ref,
            "context": None if context is None else {k: context[k] for k in ("entity", "period", "segment", "scenario")},
            "unit_ref": unit_ref,
            "unit": None if unit is None else {"numerator": unit["numerator"], "denominator": unit["denominator"]},
            "xml_lang": _lang(node), "hidden": False, "footnotes": [],
            "source": {**doc["provenance"], "line": node.line, "column": node.column, "path": node.path()},
            "available_at": doc["available_at"], "available_at_basis": doc["available_at_basis"],
            "pit_ready": doc["available_at"] is not None and not errors}


def _footnotes(root, facts_by_id, member, issues):
    """link:footnoteLink: locs point at fact ids (#id), arcs join loc labels to footnote labels."""
    count = 0
    for link in root.children:
        if link.tag != f"{{{LINK}}}footnoteLink":
            continue
        locs, notes, arcs = {}, {}, []
        for child in link.children:
            label = child.attrib.get(f"{{{XLINK}}}label")
            if child.tag == f"{{{LINK}}}loc":
                locs.setdefault(label, []).append(child.attrib.get(f"{{{XLINK}}}href", ""))
            elif child.tag == f"{{{LINK}}}footnote":
                notes.setdefault(label, []).append(child)
            elif child.tag == f"{{{LINK}}}footnoteArc":
                arcs.append(child)
        for arc in arcs:
            for href in locs.get(arc.attrib.get(f"{{{XLINK}}}from"), []):
                doc_part, _, fragment = href.partition("#")
                fact = facts_by_id.get(fragment) if doc_part in ("", member) else None
                if fact is None:
                    issues.append(ix._issue("footnote_target_not_found", "warning", member=member,
                                            line=arc.line, detail=href))
                    continue
                for note in notes.get(arc.attrib.get(f"{{{XLINK}}}to"), []):
                    text = note.text()
                    fact["footnotes"].append({"text": text, "xml_lang": _lang(note), "line": note.line,
                                              "role": note.attrib.get(f"{{{XLINK}}}role"),
                                              "arcrole": arc.attrib.get(f"{{{XLINK}}}arcrole")})
                    count += 1
    return count


def extract_document(data, *, member, provenance, members=None, available_at=None,
                     available_at_basis=None, limits=Limits(), issues=None):
    """Extract one XBRL 2.1 instance document. `members` (other ZIP members) enables role binding."""
    issues = [] if issues is None else issues
    available_at, available_at_basis = ix._availability(available_at, available_at_basis)
    root, rebinds = ix.parse_xml(data, limits)
    if root is None or root.tag != f"{{{XBRLI}}}xbrl":
        raise ExtractionError("not_an_xbrl_instance", member)
    for event in rebinds:
        issues.append(ix._issue("namespace_prefix_rebound", "warning", member=member, line=event["line"],
                                detail=f"{event['prefix']}: {event['from']} -> {event['to']}"))
    root_ns = root.ns
    contexts, units, seen, duplicate_ids = {}, {}, set(), set()
    for node in root.iter():
        ident = node.attrib.get("id")
        if ident is not None:
            # Every @id in an instance is xs:ID and must be unique in the document.
            if ident in seen:
                duplicate_ids.add(ident)
                issues.append(ix._issue("duplicate_id", "error", member=member, line=node.line, detail=ident))
            seen.add(ident)
    for node in root.children:
        if node.tag == f"{{{XBRLI}}}context":
            contexts[node.attrib.get("id")] = ctx = ix.parse_context(node, root_ns)
            for err in ctx["errors"]:
                issues.append(ix._issue(f"context_{err['code']}", "error", member=member,
                                        line=err.get("line", node.line), detail=f"{ctx['id']}: {err.get('value')}"))
        elif node.tag == f"{{{XBRLI}}}unit":
            units[node.attrib.get("id")] = unit = ix.parse_unit(node, root_ns)
            for err in unit["errors"]:
                issues.append(ix._issue(f"unit_{err['code']}", "error", member=member,
                                        line=err.get("line", node.line), detail=f"{unit['id']}: {err.get('value')}"))
    doc = {"root_ns": root_ns, "contexts": contexts, "units": units, "duplicate_ids": duplicate_ids,
           "limits": limits, "available_at": available_at, "available_at_basis": available_at_basis,
           "provenance": {**provenance, "member": member, "member_sha256": ix._sha256(data)}}
    facts, element_counts = [], {}
    for node in root.children:
        if node.tag.startswith((f"{{{XBRLI}}}", f"{{{LINK}}}")):
            name = node.tag.split("}")[1]
            element_counts[name] = element_counts.get(name, 0) + 1
            continue
        if "contextRef" not in node.attrib:
            element_counts["tuple"] = element_counts.get("tuple", 0) + 1
            issues.append(ix._issue("unsupported_tuple", "error", member=member, line=node.line,
                                    concept=node.tag, detail=f"{len(node.children)} child elements not extracted"))
            continue
        fact = _fact(node, doc)
        facts.append(fact)
        for code in fact["errors"]:
            issues.append(ix._issue(code, "error", member=member, line=node.line, fact_id=fact["fact_id"],
                                    concept=fact["concept"]["qname"], detail=fact["raw_text"][:200]))
        for code in fact["notes"]:
            issues.append(ix._issue(code, "warning", member=member, line=node.line, fact_id=fact["fact_id"],
                                    concept=fact["concept"]["qname"], detail=fact["raw_text"][:200]))
    element_counts["item"] = len(facts)
    missing_ids = sum(1 for f in facts if not f["fact_id"])
    if missing_ids:
        issues.append(ix._issue("facts_without_id", "warning", member=member, detail=str(missing_ids)))
    entities = sorted({(c["entity"] or {}).get("identifier") for c in contexts.values()} - {None})
    if len(entities) > 1:
        issues.append(ix._issue("multiple_entities", "warning", member=member, detail=", ".join(entities)))
    footnote_links = _footnotes(root, {f["fact_id"]: f for f in facts if f["fact_id"]}, member, issues)

    schema_refs = [n.attrib.get(f"{{{XLINK}}}href") for n in root.children if n.tag == f"{{{LINK}}}schemaRef"]
    roles, bindings = [], {}
    for ref in schema_refs:
        if ref is None or "://" in ref or members is None:
            issues.append(ix._issue("schema_ref_not_followed", "info", member=member, detail=str(ref)))
            continue
        found, bound = ix.parse_roles(members, ref, issues, limits)
        roles.extend(found)
        for key, value in bound.items():
            if key == "_linkbases":
                bindings.setdefault(key, []).extend(value)
            else:
                bindings.setdefault(key, {}).update(value)
    role_defs = {r["role_uri"]: r["definition"] for r in roles}
    for fact in facts:
        q, hits = fact["concept"], {}
        if q["namespace"]:
            hits.update(bindings.get(("ns", q["namespace"], q["local_name"]), {}))
            prefix = q["qname"].split(":")[0]
            hits.update(bindings.get(("fragment", f"{prefix}_{q['local_name']}"), {}))
        fact["presentation_roles"] = [{"role_uri": uri, "definition": role_defs.get(uri), "basis": basis}
                                      for uri, basis in sorted(hits.items())]
    return {"member": member, "document_type": "xbrl_instance", "member_sha256": doc["provenance"]["member_sha256"],
            "bytes": len(data), "element_counts": element_counts, "footnote_relations": footnote_links,
            "schema_refs": schema_refs, "presentation_linkbases": bindings.get("_linkbases", []),
            "contexts": contexts, "units": units, "roles": roles, "facts": facts,
            "duplicates": ix._duplicates(facts, contexts, units)}


def _result(accession, source_url, zip_info, available_at, available_at_basis, documents, issues, document_type):
    return {"schema": SCHEMA, "document_type": document_type, "accession": accession, "source_url": source_url,
            "zip": zip_info,
            "availability": {"available_at": available_at, "available_at_basis": available_at_basis,
                             "accepted_at": None, "pit_ready": available_at is not None,
                             "inferred_from_period_end": False, "retrieved_at_used": False},
            "task_ready": False, "documents": documents, "issues": issues,
            "counts": count_result(documents, issues)}


def extract_zip(path, *, accession, source_url=None, available_at=None, available_at_basis=None,
                limits=Limits()):
    """Extract every XBRL 2.1 instance document in an SEC ``*-xbrl.zip``; never task ready."""
    if not accession or not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession):
        raise ExtractionError("accession_required", str(accession))
    available_at, available_at_basis = ix._availability(available_at, available_at_basis)
    issues = []
    zip_info, members = ix.read_zip(path, limits, issues)
    inline, instance = classify_members(members)
    if inline:
        raise ExtractionError("inline_document_present", ", ".join(inline))
    if not instance:
        issues.append(ix._issue("no_xbrl_instance_document", "error"))
    if len(instance) > 1:
        issues.append(ix._issue("multiple_instance_documents", "warning", detail=", ".join(instance)))
    provenance = {"accession": accession, "zip_sha256": zip_info["sha256"], "source_url": source_url}
    documents = [extract_document(members[name], member=name, provenance=provenance, members=members,
                                  available_at=available_at, available_at_basis=available_at_basis,
                                  limits=limits, issues=issues) for name in instance]
    return _result(accession, source_url, zip_info, available_at, available_at_basis, documents, issues,
                   "xbrl_instance")


def extract_any_zip(path, *, accession, source_url=None, available_at=None, available_at_basis=None,
                    limits=Limits()):
    """Dispatcher: inline-only ZIPs go to inline_xbrl.extract_zip, instance-only ZIPs to
    extract_zip. Mixed ZIPs are refused rather than choosing one representation."""
    issues = []
    _, members = ix.read_zip(path, limits, issues)
    inline, instance = classify_members(members)
    if inline and instance:
        raise ExtractionError("mixed_inline_and_instance_refused",
                              f"inline: {', '.join(inline)}; instance: {', '.join(instance)}")
    if instance:
        return extract_zip(path, accession=accession, source_url=source_url, available_at=available_at,
                           available_at_basis=available_at_basis, limits=limits)
    result = ix.extract_zip(path, accession=accession, source_url=source_url, available_at=available_at,
                            available_at_basis=available_at_basis, limits=limits)
    result["document_type"] = "inline_xbrl"
    return result


def count_result(documents, issues):
    facts = [f for d in documents for f in d["facts"]]
    counts = {"instance_documents": len(documents), "inline_documents": 0, "facts": len(facts),
              "contexts": sum(len(d["contexts"]) for d in documents),
              "dimensional_contexts": sum(1 for d in documents for c in d["contexts"].values()
                                          if c["segment"] or c["scenario"]),
              "units": sum(len(d["units"]) for d in documents),
              "presentation_roles": sum(len(d["roles"]) for d in documents),
              "facts_with_presentation_role": sum(1 for f in facts if f["presentation_roles"]),
              "facts_with_footnote": sum(1 for f in facts if f["footnotes"]),
              "pit_ready_facts": sum(1 for f in facts if f["pit_ready"])}
    for kind in (NUMERIC, NON_NUMERIC):
        subset = [f for f in facts if f["kind"] == kind]
        counts[kind] = {"total": len(subset), "by_status": ix._tally(f["status"] for f in subset)}
    counts["duplicate_groups"] = ix._tally(g["status"] for d in documents for g in d["duplicates"])
    counts["issues_by_code"] = ix._tally(i["code"] for i in issues)
    return counts
