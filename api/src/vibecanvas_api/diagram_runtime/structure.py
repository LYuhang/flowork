"""Page-scoped inspection of native draw.io XML, shared with Preview."""

import base64
import binascii
import math
from urllib.parse import unquote_to_bytes
import zlib

from lxml import etree as ET

# Same decompression safety boundary as the retired upstream page reader.
MAX_INFLATED_BYTES = 64 * 1024 * 1024


def _xml(data):
    parser = ET.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)
    root = ET.fromstring(data, parser=parser)
    if root.getroottree().docinfo.doctype or any(isinstance(node, ET._Entity) for node in root.iter()):
        raise ValueError("DOCTYPE and ENTITY declarations are not allowed.")
    return root


def _tag(node):
    return ET.QName(node).localname if isinstance(node.tag, str) else ""


def parse_pages(data):
    """Return immutable-source page metadata and decoded model elements."""
    root = _xml(data)
    if _tag(root) == "mxGraphModel":
        return [{"page": 1, "id": None, "name": "Page 1", "compressed": False, "model": root}]
    if _tag(root) != "mxfile":
        raise ValueError("Expected an mxGraphModel or mxfile root element.")
    diagrams = [node for node in root if _tag(node) == "diagram"]
    if not diagrams:
        raise ValueError("The mxfile contains no diagram pages.")
    pages = []
    for number, diagram in enumerate(diagrams, 1):
        children = [node for node in diagram if isinstance(node.tag, str)]
        compressed = False
        try:
            if children:
                if len(children) != 1 or _tag(children[0]) != "mxGraphModel":
                    raise ValueError("A page must contain exactly one mxGraphModel.")
                model = children[0]
            else:
                body = (diagram.text or "").strip()
                if not body:
                    raise ValueError("The page has no mxGraphModel content.")
                if body.startswith("<"):
                    decoded = body.encode()
                else:
                    compressed = True
                    inflater = zlib.decompressobj(-15)
                    decoded = inflater.decompress(base64.b64decode("".join(body.split()), validate=True), MAX_INFLATED_BYTES + 1)
                    if len(decoded) > MAX_INFLATED_BYTES or inflater.unconsumed_tail:
                        raise ValueError("The decompressed page exceeds the existing 64 MiB safety limit.")
                    if not inflater.eof or inflater.unused_data:
                        raise ValueError("Invalid compressed page stream.")
                    decoded = unquote_to_bytes(decoded.decode("utf-8"))
                model = _xml(decoded)
                if _tag(model) != "mxGraphModel":
                    raise ValueError("Decoded page must contain an mxGraphModel.")
        except (ValueError, ET.XMLSyntaxError, zlib.error, binascii.Error, UnicodeError) as exc:
            raise ValueError(f"Page {number}: {exc}") from exc
        pages.append({"page": number, "id": diagram.get("id"), "name": diagram.get("name") or f"Page {number}", "compressed": compressed, "model": model})
    return pages


def inspect_structure(data):
    errors, warnings, details = [], [], []

    def issue(target, code, message, page=None, cell=None):
        target.append({"code": code, "message": message, **({"page": page} if page else {}), **({"cell": cell} if cell else {})})

    try:
        pages = parse_pages(data)
    except (ValueError, ET.XMLSyntaxError) as exc:
        message = str(exc)
        code = "unsafe-xml-declaration" if "DOCTYPE" in message or "ENTITY" in message else "invalid-drawio-root" if "root element" in message else "invalid-drawio-xml"
        issue(errors, code, message)
        return errors, warnings, details
    page_ids = set()
    for page in pages:
        number, model = page["page"], page["model"]
        if page["id"]:
            if page["id"] in page_ids:
                issue(errors, "duplicate-page-id", "Page IDs must be unique within the file.", number)
            page_ids.add(page["id"])
        roots = [node for node in model if _tag(node) == "root"]
        cells = [node for node in model.iter() if _tag(node) == "mxCell"]
        detail = {key: value for key, value in page.items() if key != "model"}
        detail.update(cells=len(cells), vertices=sum(c.get("vertex") == "1" for c in cells), edges=sum(c.get("edge") == "1" for c in cells))
        details.append(detail)
        if len(roots) != 1:
            issue(errors, "invalid-model-root", "Each mxGraphModel must contain exactly one root element.", number)
        if not cells:
            issue(errors, "missing-root-cells", "The page contains no mxCell root/layer cells.", number)
        identifiers, parents = {}, {}
        for cell in cells:
            owner = cell.getparent()
            identifier = owner.get("id") if owner is not None and _tag(owner) in {"object", "UserObject"} else cell.get("id")
            if not identifier:
                issue(errors, "missing-cell-id", "Every mxCell or its object wrapper needs an ID.", number)
                continue
            if identifier in identifiers:
                issue(errors, "duplicate-drawio-cell-id", f"Duplicate mxCell ID in this page: {identifier}", number, identifier)
            identifiers[identifier] = cell
            if cell.get("parent"):
                parents[identifier] = cell.get("parent")
        for identifier, cell in identifiers.items():
            for field in ("parent", "source", "target"):
                reference = cell.get(field)
                if reference and reference not in identifiers:
                    issue(errors, "dangling-drawio-terminal" if field != "parent" else "dangling-parent", f"{field} references missing cell {reference} in this page.", number, identifier)
            geometry = next((node for node in cell if _tag(node) == "mxGeometry"), None)
            if geometry is None and (cell.get("vertex") == "1" or cell.get("edge") == "1"):
                issue(warnings, "missing-geometry", "Cell has no mxGeometry; check its rendered placement.", number, identifier)
            if geometry is not None:
                for node in geometry.iter():
                    for field in ("x", "y", "width", "height"):
                        value = node.get(field)
                        if value is None:
                            continue
                        try:
                            valid = math.isfinite(float(value)) and (field not in {"width", "height"} or float(value) >= 0)
                        except ValueError:
                            valid = False
                        if not valid:
                            issue(errors, "invalid-geometry", f"Geometry {field} must be finite; dimensions must be non-negative.", number, identifier)
        visited = set()
        for identifier in parents:
            chain, cursor = set(), identifier
            while cursor in parents and cursor not in visited:
                if cursor in chain:
                    issue(errors, "parent-cycle", "Cell parent references contain a cycle.", number, cursor)
                    break
                chain.add(cursor)
                cursor = parents[cursor]
            visited.update(chain)
        if not detail["vertices"] and not detail["edges"]:
            issue(warnings, "empty-page", "This page has no visible vertices or edges.", number)
    return errors, warnings, details
