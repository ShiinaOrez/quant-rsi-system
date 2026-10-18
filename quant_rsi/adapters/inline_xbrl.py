"""Conservative inline XBRL 1.1 fact extraction from SEC filing ZIPs (standard library only).

Every ix:nonFraction and ix:nonNumeric becomes one fact record that keeps its raw text, QNames
with namespaces, resolved context (entity, period, dimensions) and unit (numerator and
denominator), fact ID and source location (member, line, column, element path), plus the
accession and the sha256 of the ZIP and the member. Numbers are exact Decimals. Anything this
module cannot convert under a cited rule keeps its raw text and an error instead of a value:

* transformations: ixt:num-dot-decimal and ixt:fixed-zero (XBRL Transformation Registry 4,
  REC 2020-02-12), Registry 3 numdotdecimal/zerodash, and a bounded SEC numwordsen
  subset: no/none, zero through nineteen, and the single tens twenty through ninety.
  Compound number words and every other format remain unsupported;
* value = transform(text) * 10**scale, negated when sign="-" (Inline XBRL 1.1, 10.1/10.2.3).
  A nonFraction holds exactly one text node or one nested nonFraction; markup inside it (for
  example a footnote marker or ix:exclude) is an error, never stripped into a number;
* facts with the same concept, equal context and equal unit are grouped and reported as
  consistent or inconsistent; nothing is chosen, overwritten or labelled latest;
* time is never inferred from period ends or retrieval time. Without a caller-supplied,
  timezone-aware available_at every fact has available_at=None and pit_ready=False, and the
  result is never task ready.

ZIP members are read one at a time with size caps (no extractall). XML with a DOCTYPE or entity
declaration is refused. Linked taxonomies outside the ZIP are never fetched.
"""
import hashlib
import io
import os
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import PurePosixPath
from xml.parsers import expat

SCHEMA = "inline-xbrl-facts.v1"
IX = "http://www.xbrl.org/2013/inlineXBRL"
IX_UNSUPPORTED = ("http://www.xbrl.org/2008/inlineXBRL",)
XBRLI = "http://www.xbrl.org/2003/instance"
XBRLDI = "http://xbrl.org/2006/xbrldi"
LINK = "http://www.xbrl.org/2003/linkbase"
XLINK = "http://www.w3.org/1999/xlink"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
XS = "http://www.w3.org/2001/XMLSchema"
XML_NS = "http://www.w3.org/XML/1998/namespace"
TR4 = "http://www.xbrl.org/inlineXBRL/transformation/2020-02-12"
TR3 = "http://www.xbrl.org/inlineXBRL/transformation/2015-02-26"
SEC_TR = "http://www.sec.gov/inlineXBRL/transformation/2015-08-31"
PRESENTATION_REF = "http://www.xbrl.org/2003/role/presentationLinkbaseRef"
# Numeric fact kinds: inline ix:nonFraction and traditional instance items (xbrl_instance.py).
NUMERIC_KINDS = ("nonFraction", "numericItem")


@dataclass(frozen=True)
class Limits:
    max_zip_bytes: int = 64 << 20
    max_members: int = 1000
    max_member_bytes: int = 32 << 20
    max_total_bytes: int = 256 << 20
    max_ratio: int = 200          # uncompressed / compressed, per member
    max_elements: int = 2_000_000
    max_scale: int = 30
    max_digits: int = 60


class ExtractionError(Exception):
    """The whole input is refused; no partial facts are returned."""

    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


class TransformError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code, self.detail = code, detail


# ---------------------------------------------------------------- XML

class Node:
    __slots__ = ("tag", "attrib", "children", "parts", "parent", "line", "column", "ns",
                 "index", "has_comment")

    def __init__(self, tag, attrib, parent, line, column, ns):
        self.tag, self.attrib, self.parent = tag, attrib, parent
        self.line, self.column, self.ns = line, column, ns
        self.children, self.parts, self.has_comment = [], [], False
        self.index = 1 + sum(1 for c in parent.children if c.tag == tag) if parent else 1

    def iter(self):
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.children))

    def find_all(self, tag):
        return [n for n in self.iter() if n.tag == tag]

    def find(self, tag):
        return next((c for c in self.children if c.tag == tag), None)

    def text(self, skip=()):
        out = []
        for part in self.parts:
            if isinstance(part, str):
                out.append(part)
            elif part.tag not in skip:
                out.append(part.text(skip))
        return "".join(out)

    def path(self):
        steps, node = [], self
        while node is not None:
            uri, _, local = node.tag[1:].partition("}") if node.tag.startswith("{") else ("", "", node.tag)
            name = f"ix:{local}" if uri == IX else local
            steps.append(f"{name}[{node.index}]")
            node = node.parent
        return "/" + "/".join(reversed(steps))


def _clark(name):
    uri, sep, local = name.rpartition("}")
    return f"{{{uri}}}{local}" if sep else name


def parse_xml(data, limits=Limits()):
    """Parse bytes into Nodes with in-scope namespace maps and line/column; refuse DTDs."""
    parser = expat.ParserCreate(namespace_separator="}")
    parser.buffer_text = True
    state = {"root": None, "count": 0}
    stack, pending, rebinds = [], [], []

    def refuse(code):
        def handler(*_):
            raise ExtractionError(code, f"line {parser.CurrentLineNumber}")
        return handler

    def start_ns(prefix, uri):
        pending.append((prefix or "", uri or ""))

    def start(name, attrs):
        state["count"] += 1
        if state["count"] > limits.max_elements:
            raise ExtractionError("too_many_elements", str(limits.max_elements))
        parent = stack[-1] if stack else None
        scope = parent.ns if parent else {"xml": XML_NS}
        if pending:
            scope = dict(scope)
            for prefix, uri in pending:
                if prefix in scope and scope[prefix] != uri:
                    rebinds.append({"prefix": prefix, "from": scope[prefix], "to": uri,
                                    "line": parser.CurrentLineNumber})
                scope[prefix] = uri
            pending.clear()
        node = Node(_clark(name), {_clark(k): v for k, v in attrs.items()}, parent,
                    parser.CurrentLineNumber, parser.CurrentColumnNumber + 1, scope)
        if parent is None:
            state["root"] = node
        else:
            parent.children.append(node)
            parent.parts.append(node)
        stack.append(node)

    def end(_name):
        stack.pop()

    def chars(text):
        if stack:
            parts = stack[-1].parts
            if parts and isinstance(parts[-1], str):
                parts[-1] += text
            else:
                parts.append(text)

    def comment(_text):
        if stack:
            stack[-1].has_comment = True

    parser.StartNamespaceDeclHandler = start_ns
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    parser.CommentHandler = comment
    parser.StartDoctypeDeclHandler = refuse("doctype_refused")
    parser.EntityDeclHandler = refuse("entity_declaration_refused")
    parser.UnparsedEntityDeclHandler = refuse("entity_declaration_refused")
    parser.ExternalEntityRefHandler = refuse("external_entity_refused")
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise ExtractionError("xml_parse_error", str(exc)) from None
    return state["root"], rebinds


def _collapse(text):
    return " ".join(re.split(r"[ \t\r\n]+", text)).strip()


def resolve_qname(text, node, root_ns, *, allow_default=False):
    """Resolve a QName; unit measures may use the declared default namespace.

    XBRL 2.1 section 4.8.2 uses xsd:QName, including unprefixed names. No namespace
    is guessed from the word 'shares' or 'pure'. Other call sites remain conservative.
    """
    value = _collapse(text or "")
    prefix, sep, local = value.partition(":")
    if not sep and allow_default and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", value):
        uri = node.ns.get("") or None
        qname = {"qname": value, "namespace": uri, "local_name": value}
        if uri is None:
            return qname, "unbound_default_namespace"
        if root_ns.get("") != uri:
            return qname, "namespace_default_rebound"
        return qname, None
    if not sep or not prefix or not local or ":" in local:
        return {"qname": value, "namespace": None, "local_name": None}, "unprefixed_or_malformed_qname"
    uri = node.ns.get(prefix)
    qname = {"qname": value, "namespace": uri, "local_name": local}
    if uri is None:
        return qname, "unbound_prefix"
    if root_ns.get(prefix) != uri:
        return qname, "namespace_prefix_rebound"
    return qname, None


# ---------------------------------------------------------------- transformations

_NO_FORMAT = re.compile(r"\+?([0-9]+(\.[0-9]*)?|\.[0-9]+)")
_NUM_DOT_DECIMAL = re.compile("[0-9][0-9,  ]*(\\.[0-9][0-9  ]*)?")


def _no_format(raw):
    text = _collapse(raw)
    if not _NO_FORMAT.fullmatch(text):
        raise TransformError("invalid_number_without_format", raw)
    return Decimal(text)


def _num_dot_decimal(raw):
    # TR4 numDotDecimalType: comma, space or no-break space digit separators, dot fraction
    # separator followed by at least one digit, no signs or exponents.
    text = _collapse(raw)
    if not _NUM_DOT_DECIMAL.fullmatch(text) or not text[-1].isdigit():
        raise TransformError("invalid_transform_input", raw)
    return Decimal(re.sub("[,  ]", "", text))


def _fixed_zero(_raw):
    return Decimal(0)


def _tr3_numdotdecimal(raw):
    # Registry 3 numdotdecimalType requires groups of three; it is stricter
    # than Registry 4. Match the entire input, including its fractional part.
    text = _collapse(raw)
    if not re.fullmatch(r"[0-9]{1,3}([, \u00a0]?[0-9]{3})*(\.[0-9]+)?", text):
        raise TransformError("invalid_transform_input", raw)
    return Decimal(re.sub("[, \u00a0]", "", text))


def _tr3_zerodash(raw):
    # Exact Registry 3 zerodashType enumeration, after XML whitespace collapse.
    text = _collapse(raw)
    if len(text) != 1 or text not in "-\u058a\u05be\u2010\u2011\u2012\u2013\u2014\u2015\ufe58\ufe63\uff0d":
        raise TransformError("invalid_transform_input", raw)
    return Decimal(0)


# The subset is explicit; no approximate conversion or token dropping is allowed.
_ATOMIC_NUMBER_WORDS = {word: value for value, word in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen".split())}
_ATOMIC_NUMBER_WORDS.update({"no": 0, "none": 0, "twenty": 20, "thirty": 30, "forty": 40,
                             "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90})


def _numwords_atomic(raw):
    # Deliberately bounded subset of SEC numwordsen, not its compound-word grammar.
    text = _collapse(raw).casefold()
    if text in _ATOMIC_NUMBER_WORDS:
        return Decimal(_ATOMIC_NUMBER_WORDS[text])
    raise TransformError("unsupported_transform_input", raw)


TRANSFORMS = {(TR4, "num-dot-decimal"): _num_dot_decimal, (TR4, "fixed-zero"): _fixed_zero,
              (TR3, "numdotdecimal"): _tr3_numdotdecimal, (TR3, "zerodash"): _tr3_zerodash,
              (SEC_TR, "numwordsen"): _numwords_atomic}


def plain(value):
    return None if value is None else format(value, "f")


# ---------------------------------------------------------------- ZIP

def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _unsafe_name(name):
    path = PurePosixPath(name)
    return (name.startswith(("/", "\\")) or "\\" in name or ".." in path.parts
            or re.match(r"^[A-Za-z]:", name) is not None)


def read_zip(path, limits, issues):
    """Read each member with caps. Returns (zip info dict, {name: bytes})."""
    if "://" in str(path):
        raise ExtractionError("external_source_refused", str(path))
    size = os.path.getsize(path)
    if size > limits.max_zip_bytes:
        raise ExtractionError("zip_too_large", f"{size} > {limits.max_zip_bytes}")
    with open(path, "rb") as handle:
        raw = handle.read(limits.max_zip_bytes + 1)
    if len(raw) > limits.max_zip_bytes:
        raise ExtractionError("zip_too_large", str(len(raw)))
    try:
        # Parse the same bytes that were hashed.
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ExtractionError("bad_zip", str(exc)) from None
    members, listing, total = {}, [], 0
    with archive:
        infos = archive.infolist()
        if len(infos) > limits.max_members:
            raise ExtractionError("too_many_members", str(len(infos)))
        names = [i.filename for i in infos]
        if len(names) != len(set(names)):
            raise ExtractionError("duplicate_member_names")
        for info in infos:
            entry = {"name": info.filename, "bytes": info.file_size, "compressed": info.compress_size}
            listing.append(entry)
            if info.is_dir():
                entry["status"] = "directory"
                continue
            problem = None
            if _unsafe_name(info.filename):
                problem = "unsafe_member_name"
            elif info.flag_bits & 0x1:
                problem = "encrypted_member"
            elif info.file_size > limits.max_member_bytes:
                problem = "member_too_large"
            elif info.file_size > limits.max_ratio * max(info.compress_size, 1) and info.file_size > 1 << 20:
                problem = "compression_ratio_exceeded"
            elif total + info.file_size > limits.max_total_bytes:
                problem = "total_uncompressed_limit"
            if problem is None:
                try:
                    with archive.open(info) as stream:
                        data = stream.read(limits.max_member_bytes + 1)
                except (zipfile.BadZipFile, ValueError, EOFError, OSError, RuntimeError) as error:
                    raise ExtractionError("corrupt_zip_member", f"{info.filename}: {error}") from None
                if len(data) > limits.max_member_bytes or len(data) != info.file_size:
                    problem = "member_size_mismatch"
            if problem:
                entry["status"] = "refused"
                issues.append(_issue(problem, "error", member=info.filename, detail=str(info.file_size)))
                continue
            total += len(data)
            entry.update(status="read", sha256=_sha256(data))
            members[info.filename] = data
    return {"path": str(path), "sha256": _sha256(raw), "bytes": len(raw), "members": listing}, members


def _issue(code, severity, **fields):
    return {"code": code, "severity": severity, **{k: v for k, v in fields.items() if v is not None}}


# ---------------------------------------------------------------- contexts, units, roles

def _qn_entry(text, node, root_ns, errors, *, allow_default=False):
    qname, error = resolve_qname(text, node, root_ns, allow_default=allow_default)
    if error:
        errors.append({"code": error, "value": qname["qname"], "line": node.line})
    return qname


def _dimensions(container, kind, root_ns, errors):
    dims = []
    if container is None:
        return dims
    for child in container.children:
        if child.tag == f"{{{XBRLDI}}}explicitMember":
            dims.append({"kind": "explicit",
                         "dimension": _qn_entry(child.attrib.get("dimension"), child, root_ns, errors),
                         "member": _qn_entry(child.text(), child, root_ns, errors)})
        elif child.tag == f"{{{XBRLDI}}}typedMember":
            dims.append({"kind": "typed",
                         "dimension": _qn_entry(child.attrib.get("dimension"), child, root_ns, errors),
                         "raw": child.text().strip()})
        else:
            errors.append({"code": f"unsupported_{kind}_content", "value": child.tag, "line": child.line})
    return dims


def parse_context(node, root_ns):
    errors = []
    entity, period = node.find(f"{{{XBRLI}}}entity"), node.find(f"{{{XBRLI}}}period")
    ident = entity.find(f"{{{XBRLI}}}identifier") if entity is not None else None
    out = {"id": node.attrib.get("id"), "line": node.line,
           "entity": None if ident is None else {"scheme": ident.attrib.get("scheme"),
                                                 "identifier": ident.text().strip()}}
    if ident is None:
        errors.append({"code": "missing_entity_identifier"})
    values = {} if period is None else {c.tag.split("}")[-1]: c.text().strip() for c in period.children}
    if set(values) == {"instant"}:
        out["period"] = {"type": "instant", "instant": values["instant"]}
    elif set(values) == {"startDate", "endDate"}:
        out["period"] = {"type": "duration", "start": values["startDate"], "end": values["endDate"]}
    elif set(values) == {"forever"}:
        out["period"] = {"type": "forever"}
    else:
        out["period"] = None
        errors.append({"code": "invalid_period", "value": sorted(values)})
    out["segment"] = _dimensions(entity.find(f"{{{XBRLI}}}segment") if entity is not None else None,
                                 "segment", root_ns, errors)
    out["scenario"] = _dimensions(node.find(f"{{{XBRLI}}}scenario"), "scenario", root_ns, errors)
    out["errors"] = errors
    return out


def parse_unit(node, root_ns):
    errors = []

    def measures(parent):
        return [_qn_entry(m.text(), m, root_ns, errors, allow_default=True) for m in parent.children
                if m.tag == f"{{{XBRLI}}}measure"] if parent is not None else []

    divide = node.find(f"{{{XBRLI}}}divide")
    if divide is not None:
        num = measures(divide.find(f"{{{XBRLI}}}unitNumerator"))
        den = measures(divide.find(f"{{{XBRLI}}}unitDenominator"))
        if not num or not den:
            errors.append({"code": "invalid_divide_unit"})
    else:
        num, den = measures(node), []
        if not num:
            errors.append({"code": "unit_without_measure"})
    return {"id": node.attrib.get("id"), "line": node.line, "numerator": num, "denominator": den,
            "errors": errors}


def _qkey(q):
    return (q["namespace"], q["local_name"]) if q["namespace"] else ("?", q["qname"])


def context_signature(ctx):
    def dims(items):
        return tuple(sorted((_qkey(d["dimension"]), _qkey(d["member"]) if d["kind"] == "explicit"
                             else ("typed", d["raw"])) for d in items))
    period = tuple(sorted((ctx["period"] or {}).items()))
    entity = tuple(sorted((ctx["entity"] or {}).items()))
    return (entity, period, dims(ctx["segment"]), dims(ctx["scenario"]))


def unit_signature(unit):
    return (tuple(sorted(_qkey(q) for q in unit["numerator"])),
            tuple(sorted(_qkey(q) for q in unit["denominator"])))


def parse_roles(members, schema_name, issues, limits):
    """Presentation roles from the local schema's linkbaseRefs; binds by concept, not by context."""
    roles, local_ids = {}, {}
    if schema_name not in members:
        issues.append(_issue("schema_not_in_zip", "warning", detail=schema_name))
        return [], {}
    schema, _ = parse_xml(members[schema_name], limits)
    target = schema.attrib.get("targetNamespace")
    for el in schema.find_all(f"{{{XS}}}element"):
        if el.attrib.get("id") and el.attrib.get("name"):
            local_ids[el.attrib["id"]] = (target, el.attrib["name"])
    for rt in schema.find_all(f"{{{LINK}}}roleType"):
        definition = rt.find(f"{{{LINK}}}definition")
        roles[rt.attrib.get("roleURI")] = {"role_uri": rt.attrib.get("roleURI"), "role_id": rt.attrib.get("id"),
                                           "definition": definition.text() if definition is not None else None,
                                           "concepts": []}
    refs = [r.attrib.get(f"{{{XLINK}}}href") for r in schema.find_all(f"{{{LINK}}}linkbaseRef")
            if r.attrib.get(f"{{{XLINK}}}role") == PRESENTATION_REF]
    if not refs:
        issues.append(_issue("no_presentation_linkbase_ref", "warning", member=schema_name))
    bindings = {}
    for ref in refs:
        if ref not in members:
            issues.append(_issue("presentation_linkbase_not_in_zip", "warning", detail=ref))
            continue
        pre, _ = parse_xml(members[ref], limits)
        for link in pre.find_all(f"{{{LINK}}}presentationLink"):
            uri = link.attrib.get(f"{{{XLINK}}}role")
            role = roles.setdefault(uri, {"role_uri": uri, "role_id": None, "definition": None, "concepts": []})
            for loc in link.children:
                if loc.tag != f"{{{LINK}}}loc":
                    continue
                href = loc.attrib.get(f"{{{XLINK}}}href", "")
                doc, _, fragment = href.partition("#")
                if doc == schema_name and fragment in local_ids:
                    ns, name = local_ids[fragment]
                    key, basis = ("ns", ns, name), "local_schema_element_id"
                else:
                    # External taxonomy schemas are not fetched, so prefix_LocalName is unverified.
                    key, basis = ("fragment", fragment), "external_fragment_convention_unverified"
                entry = {"href": href, "basis": basis}
                if entry not in role["concepts"]:
                    role["concepts"].append(entry)
                bindings.setdefault(key, {})[uri] = basis
        bindings.setdefault("_linkbases", []).append({"member": ref, "sha256": _sha256(members[ref])})
    return list(roles.values()), bindings


# ---------------------------------------------------------------- facts

def _int_attr(value, pattern=r"-?[0-9]+"):
    return int(value) if value is not None and re.fullmatch(pattern, value) else None


def _numeric(node, doc, errors):
    a = node.attrib
    nil = a.get(f"{{{XSI}}}nil") in ("true", "1")
    nested = None
    stray_text = any(isinstance(p, str) and p.strip() for p in node.parts)
    if nil:
        if node.children or stray_text:
            errors.append("nil_with_content")
        if node.parent is not None and node.parent.tag == f"{{{IX}}}nonFraction":
            errors.append("nil_nested_in_nonfraction")
        raw = node.text()
    elif node.children:
        if len(node.children) != 1 or node.children[0].tag != f"{{{IX}}}nonFraction" or stray_text:
            errors.append("invalid_nonfraction_content")
            raw = node.text()
        else:
            nested = node.children[0]
            raw = nested.text()
            for attr in ("format", "scale", "unitRef"):
                if nested.attrib.get(attr) != a.get(attr):
                    errors.append("nested_property_mismatch")
                    break
    else:
        raw = node.text()
        if not raw:
            errors.append("empty_value")
    if node.has_comment:
        errors.append("comment_inside_value")

    decimals, precision = a.get("decimals"), a.get("precision")
    if not nil:
        if (decimals is None) == (precision is None):
            errors.append("decimals_or_precision_required")
        if decimals is not None and decimals != "INF" and _int_attr(decimals) is None:
            errors.append("invalid_decimals")
        if precision is not None and precision != "INF" and _int_attr(precision, r"[0-9]+") is None:
            errors.append("invalid_precision")
    scale = 0
    if a.get("scale") is not None:
        scale = _int_attr(a["scale"])
        if scale is None or abs(scale) > doc["limits"].max_scale:
            errors.append("invalid_scale")
            scale = None
    sign = a.get("sign")
    if sign not in (None, "-"):
        errors.append("invalid_sign")

    fmt, transform = None, _no_format
    if a.get("format") is not None:
        fmt, error = resolve_qname(a["format"], node, doc["root_ns"])
        transform = TRANSFORMS.get((fmt["namespace"], fmt["local_name"]))
        if error:
            errors.append(f"format_{error}")
        elif transform is None:
            errors.append("unsupported_format")

    value = None
    if not errors and not nil:
        try:
            base = transform(raw)
            if len(base.as_tuple().digits) > doc["limits"].max_digits:
                raise TransformError("too_many_digits", raw)
            with localcontext() as ctx:
                ctx.prec = doc["limits"].max_digits + 2 * doc["limits"].max_scale
                value = base.scaleb(scale)
                if sign == "-":
                    value = -value
            if value == 0:
                value = Decimal(0)
        except TransformError as exc:
            errors.append(exc.code)
        except InvalidOperation:
            errors.append("decimal_error")
    return {"raw_text": raw, "nil": nil, "format": fmt, "scale": a.get("scale"), "sign": sign,
            "decimals": decimals, "precision": precision, "value": plain(value),
            "nested_child_fact_id": nested.attrib.get("id") if nested is not None else None}


def _nonnumeric(node, doc, errors):
    skip = (f"{{{IX}}}exclude",)
    parts, seen, nxt = [node.text(skip)], set(), node.attrib.get("continuedAt")
    while nxt:
        if nxt in seen:
            errors.append("continuation_cycle")
            break
        seen.add(nxt)
        cont = doc["continuations"].get(nxt)
        if cont is None:
            errors.append("missing_continuation")
            break
        parts.append(cont.text(skip))
        nxt = cont.attrib.get("continuedAt")
    text = "".join(parts)
    fmt = None
    if node.attrib.get("format") is not None:
        fmt, error = resolve_qname(node.attrib["format"], node, doc["root_ns"])
        if error:
            errors.append(f"format_{error}")
    nil = node.attrib.get(f"{{{XSI}}}nil") in ("true", "1")
    return {"raw_text": text, "nil": nil, "format": fmt, "escape": node.attrib.get("escape"),
            "continued_at": node.attrib.get("continuedAt"), "text_sha256": _sha256(text.encode("utf-8")),
            # Formatted nonNumeric values (dates, booleans) are not transformed here.
            "value": None if (fmt or nil or errors) else text}


def _ancestry(node):
    hidden = excluded = False
    parent = node.parent
    while parent is not None:
        hidden |= parent.tag == f"{{{IX}}}hidden"
        excluded |= parent.tag == f"{{{IX}}}exclude"
        parent = parent.parent
    return hidden, excluded


def _fact(node, doc):
    errors = []
    kind = node.tag.split("}")[1]
    concept, error = resolve_qname(node.attrib.get("name"), node, doc["root_ns"])
    if error:
        errors.append(f"concept_{error}")
    hidden, excluded = _ancestry(node)
    if excluded:
        errors.append("fact_inside_ix_exclude")
    ctx_ref, unit_ref = node.attrib.get("contextRef"), node.attrib.get("unitRef")
    context = doc["contexts"].get(ctx_ref)
    if ctx_ref is None or ctx_ref not in doc["contexts"]:
        errors.append("missing_context")
    elif ctx_ref in doc["duplicate_ids"]:
        errors.append("ambiguous_context_id")
    elif context["errors"]:
        errors.append("invalid_context")
    unit = None
    if kind == "nonFraction":
        unit = doc["units"].get(unit_ref)
        if unit_ref is None or unit is None:
            errors.append("missing_unit")
        elif unit_ref in doc["duplicate_ids"]:
            errors.append("ambiguous_unit_id")
        elif unit["errors"]:
            errors.append("invalid_unit")
        body = _numeric(node, doc, errors)
    else:
        body = _nonnumeric(node, doc, errors)
    status = "error" if errors else "nil" if body["nil"] else "ok" if body["value"] is not None else "raw_only"
    return {"fact_id": node.attrib.get("id"), "kind": kind, "concept": concept, **body,
            "status": status, "errors": errors, "context_ref": ctx_ref,
            "context": None if context is None else {k: context[k] for k in ("entity", "period", "segment", "scenario")},
            "unit_ref": unit_ref,
            "unit": None if unit is None else {"numerator": unit["numerator"], "denominator": unit["denominator"]},
            "xml_lang": node.attrib.get(f"{{{XML_NS}}}lang"), "hidden": hidden,
            "nested_in_fact_id": node.parent.attrib.get("id") if node.parent is not None
            and node.parent.tag == f"{{{IX}}}nonFraction" else None,
            "source": {**doc["provenance"], "line": node.line, "column": node.column, "path": node.path()},
            "available_at": doc["available_at"], "available_at_basis": doc["available_at_basis"],
            "pit_ready": doc["available_at"] is not None and not errors}


def _duplicates(facts, contexts, units):
    groups = {}
    for fact in facts:
        ctx, unit = contexts.get(fact["context_ref"]), units.get(fact["unit_ref"])
        if ctx is None or (fact["kind"] in NUMERIC_KINDS and unit is None):
            continue
        key = (_qkey(fact["concept"]), repr(context_signature(ctx)),
               repr(unit_signature(unit)) if unit else None,
               fact["xml_lang"] if fact["kind"] not in NUMERIC_KINDS else None)
        groups.setdefault(key, []).append(fact)
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        values = {(f["value"], f["nil"]) for f in members}
        if any(f["status"] == "error" for f in members):
            status = "unresolved_member_error"
        elif len(values) == 1:
            status = "consistent"
        else:
            status = "inconsistent"
        out.append({"concept": members[0]["concept"]["qname"], "status": status,
                    "context_refs": sorted({f["context_ref"] for f in members}),
                    "unit_ref": members[0]["unit_ref"],
                    "facts": [{"fact_id": f["fact_id"], "value": f["value"], "raw_text": f["raw_text"][:200],
                               "decimals": f.get("decimals"), "line": f["source"]["line"]} for f in members]})
    return out


def _availability(available_at, basis):
    if available_at is None:
        if basis is not None:
            raise ExtractionError("available_at_basis_without_time")
        return None, None
    if isinstance(available_at, str):
        try:
            available_at = datetime.fromisoformat(available_at)
        except ValueError:
            raise ExtractionError("invalid_available_at", available_at) from None
    if not isinstance(available_at, datetime) or available_at.tzinfo is None \
            or available_at.tzinfo.utcoffset(available_at) is None:
        raise ExtractionError("available_at_requires_timezone", str(available_at))
    if not basis or not str(basis).strip():
        raise ExtractionError("available_at_basis_required")
    return available_at.isoformat(), str(basis)


def extract_document(data, *, member, provenance, members=None, available_at=None,
                     available_at_basis=None, limits=Limits(), issues=None):
    """Extract one inline XBRL document. `members` (other ZIP members) enables role binding."""
    issues = [] if issues is None else issues
    available_at, available_at_basis = _availability(available_at, available_at_basis)
    root, rebinds = parse_xml(data, limits)
    for event in rebinds:
        issues.append(_issue("namespace_prefix_rebound", "warning", member=member, line=event["line"],
                             detail=f"{event['prefix']}: {event['from']} -> {event['to']}"))
    root_ns = root.ns
    all_nodes = list(root.iter())
    if any(n.tag.startswith(f"{{{ns}}}") for n in all_nodes for ns in IX_UNSUPPORTED):
        raise ExtractionError("unsupported_inline_xbrl_namespace", member)
    contexts, units, seen, duplicate_ids = {}, {}, set(), set()
    for node in all_nodes:
        if node.tag in (f"{{{XBRLI}}}context", f"{{{XBRLI}}}unit"):
            ident = node.attrib.get("id")
            if ident in seen:
                duplicate_ids.add(ident)
                issues.append(_issue("duplicate_resource_id", "error", member=member, line=node.line, detail=ident))
            seen.add(ident)
            if node.tag.endswith("}context"):
                contexts[ident] = parse_context(node, root_ns)
                for err in contexts[ident]["errors"]:
                    issues.append(_issue(f"context_{err['code']}", "error", member=member,
                                         line=err.get("line", node.line), detail=f"{ident}: {err.get('value')}"))
            else:
                units[ident] = parse_unit(node, root_ns)
                for err in units[ident]["errors"]:
                    issues.append(_issue(f"unit_{err['code']}", "error", member=member,
                                         line=err.get("line", node.line), detail=f"{ident}: {err.get('value')}"))
    doc = {"root_ns": root_ns, "contexts": contexts, "units": units, "duplicate_ids": duplicate_ids,
           "continuations": {n.attrib.get("id"): n for n in all_nodes if n.tag == f"{{{IX}}}continuation"},
           "limits": limits, "available_at": available_at, "available_at_basis": available_at_basis,
           "provenance": {**provenance, "member": member, "member_sha256": _sha256(data)}}
    facts, ix_counts = [], {}
    for node in all_nodes:
        if not node.tag.startswith(f"{{{IX}}}"):
            continue
        local = node.tag.split("}")[1]
        ix_counts[local] = ix_counts.get(local, 0) + 1
        if local in ("nonFraction", "nonNumeric"):
            fact = _fact(node, doc)
            facts.append(fact)
            for code in fact["errors"]:
                issues.append(_issue(code, "error", member=member, line=node.line, fact_id=fact["fact_id"],
                                     concept=fact["concept"]["qname"], detail=fact["raw_text"][:200]))
        elif local in ("fraction", "tuple"):
            issues.append(_issue(f"unsupported_ix_{local}", "error", member=member, line=node.line,
                                 detail=node.text()[:200]))
        elif local == "exclude" and not any(
                p.tag in (f"{{{IX}}}nonNumeric", f"{{{IX}}}continuation", f"{{{IX}}}footnote")
                for p in _parents(node)):
            issues.append(_issue("misplaced_ix_exclude", "error", member=member, line=node.line))
    ids = [f["fact_id"] for f in facts if f["fact_id"]]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        issues.append(_issue("duplicate_fact_id", "error", member=member, detail=dup))
    missing_ids = sum(1 for f in facts if not f["fact_id"])
    if missing_ids:
        issues.append(_issue("facts_without_id", "warning", member=member, detail=str(missing_ids)))
    entities = sorted({(c["entity"] or {}).get("identifier") for c in contexts.values()} - {None})
    if len(entities) > 1:
        issues.append(_issue("multiple_entities", "warning", member=member, detail=", ".join(entities)))

    schema_refs = [n.attrib.get(f"{{{XLINK}}}href") for n in all_nodes if n.tag == f"{{{LINK}}}schemaRef"]
    roles, bindings = [], {}
    for ref in schema_refs:
        if ref is None or "://" in ref or members is None:
            issues.append(_issue("schema_ref_not_followed", "info", member=member, detail=str(ref)))
            continue
        found, bound = parse_roles(members, ref, issues, limits)
        roles.extend(found)
        for key, value in bound.items():
            if key == "_linkbases":
                bindings.setdefault(key, []).extend(value)
            else:
                bindings.setdefault(key, {}).update(value)
    role_defs = {r["role_uri"]: r["definition"] for r in roles}
    for fact in facts:
        q = fact["concept"]
        hits = {}
        if q["namespace"]:
            hits.update(bindings.get(("ns", q["namespace"], q["local_name"]), {}))
            prefix = q["qname"].split(":")[0]
            hits.update(bindings.get(("fragment", f"{prefix}_{q['local_name']}"), {}))
        fact["presentation_roles"] = [{"role_uri": uri, "definition": role_defs.get(uri), "basis": basis}
                                      for uri, basis in sorted(hits.items())]
    return {"member": member, "member_sha256": doc["provenance"]["member_sha256"], "bytes": len(data),
            "ix_element_counts": ix_counts, "schema_refs": schema_refs,
            "presentation_linkbases": bindings.get("_linkbases", []),
            "contexts": contexts, "units": units, "roles": roles, "facts": facts,
            "duplicates": _duplicates(facts, contexts, units)}


def _parents(node):
    parent = node.parent
    while parent is not None:
        yield parent
        parent = parent.parent


def extract_zip(path, *, accession, source_url=None, available_at=None, available_at_basis=None,
                limits=Limits()):
    """Extract every inline XBRL document in an SEC ``*-xbrl.zip``.

    The result is never task ready. Facts become PIT-eligible only with an explicit,
    timezone-aware ``available_at`` and a written ``available_at_basis``.
    """
    if not accession or not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession):
        raise ExtractionError("accession_required", str(accession))
    available_at, available_at_basis = _availability(available_at, available_at_basis)
    issues = []
    zip_info, members = read_zip(path, limits, issues)
    provenance = {"accession": accession, "zip_sha256": zip_info["sha256"], "source_url": source_url}
    inline, documents = [], []
    for name, data in members.items():
        if not name.lower().endswith((".htm", ".html", ".xhtml")):
            continue
        if IX.encode() in data:
            inline.append(name)
        elif any(ns.encode() in data for ns in IX_UNSUPPORTED):
            issues.append(_issue("unsupported_inline_xbrl_namespace", "error", member=name))
    if len(inline) > 1:
        # Inline document sets share resources across members; not supported here.
        raise ExtractionError("inline_document_set_unsupported", ", ".join(sorted(inline)))
    if not inline:
        issues.append(_issue("no_inline_xbrl_document", "error"))
    for name in inline:
        documents.append(extract_document(members[name], member=name, provenance=provenance, members=members,
                                          available_at=available_at, available_at_basis=available_at_basis,
                                          limits=limits, issues=issues))
    facts = [f for d in documents for f in d["facts"]]
    return {"schema": SCHEMA, "accession": accession, "source_url": source_url, "zip": zip_info,
            "availability": {"available_at": available_at, "available_at_basis": available_at_basis,
                             "accepted_at": None, "pit_ready": available_at is not None,
                             "inferred_from_period_end": False, "retrieved_at_used": False},
            "task_ready": False, "documents": documents, "issues": issues,
            "counts": count_result(documents, issues)}


def count_result(documents, issues):
    facts = [f for d in documents for f in d["facts"]]
    counts = {"inline_documents": len(documents), "facts": len(facts),
              "contexts": sum(len(d["contexts"]) for d in documents),
              "dimensional_contexts": sum(1 for d in documents for c in d["contexts"].values()
                                          if c["segment"] or c["scenario"]),
              "units": sum(len(d["units"]) for d in documents),
              "presentation_roles": sum(len(d["roles"]) for d in documents),
              "facts_with_presentation_role": sum(1 for f in facts if f["presentation_roles"]),
              "pit_ready_facts": sum(1 for f in facts if f["pit_ready"])}
    for kind in ("nonFraction", "nonNumeric"):
        subset = [f for f in facts if f["kind"] == kind]
        counts[kind] = {"total": len(subset), "by_status": _tally(f["status"] for f in subset),
                        "by_format": _tally((f["format"] or {}).get("qname") or "(none)" for f in subset)}
    counts["duplicate_groups"] = _tally(g["status"] for d in documents for g in d["duplicates"])
    counts["issues_by_code"] = _tally(i["code"] for i in issues)
    return counts


def _tally(items):
    out = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return dict(sorted(out.items()))


# Also the dated 2011-2018 releases and the XBRL US 2009 release found in traditional instances.
_US_GAAP_NS = re.compile(r"^(http://fasb\.org/us-gaap/\d{4}(-\d{2}-\d{2})?|http://xbrl\.us/us-gaap/2009-01-31)$")


def compare_filings(results, match="qname"):
    """Same concept/context/unit across filings. Differences are reported, never resolved.

    match="qname" compares exact namespaces. match="us_gaap_local_name" treats us-gaap concepts
    of different taxonomy years with the same local name as one concept; that is a naming
    convention, not something this module verifies.
    """
    require_us_gaap = match == "us_gaap_local_name"
    if match not in ("qname", "us_gaap_local_name"):
        raise ValueError(match)

    def concept_key(q):
        if require_us_gaap:
            return ("us-gaap", q["local_name"]) if _US_GAAP_NS.match(q["namespace"] or "") else None
        return _qkey(q)

    groups = {}
    for result in results:
        for doc in result["documents"]:
            for fact in doc["facts"]:
                if fact["kind"] not in NUMERIC_KINDS or fact["status"] != "ok":
                    continue
                concept = concept_key(fact["concept"])
                if concept is None:
                    continue
                ctx, unit = doc["contexts"][fact["context_ref"]], doc["units"][fact["unit_ref"]]
                key = (concept, repr(context_signature(ctx)), repr(unit_signature(unit)))
                groups.setdefault(key, {}).setdefault(result["accession"], set()).add(fact["value"])
    out = {"match": match, "shared_keys": 0, "same": 0, "differs": []}
    for key, by_acc in groups.items():
        if len(by_acc) < 2:
            continue
        out["shared_keys"] += 1
        values = set().union(*by_acc.values())
        if len(values) == 1:
            out["same"] += 1
        else:
            out["differs"].append({"concept": f"{key[0][0]}#{key[0][1]}", "context_signature": key[1],
                                   "values_by_accession": {a: sorted(v) for a, v in by_acc.items()}})
    return out
