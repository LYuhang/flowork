"""Derive least-privilege VFS read rules from stored HTML definitions."""

from __future__ import annotations

from html.parser import HTMLParser
import json
import posixpath
import re
from typing import Any
from urllib.parse import unquote, urlsplit

import markdown as markdown_lib


_ROOTS = ("/data/", "/memory/", "/logs/", "/chats/", "/mount/", "/run/")
_QUOTED_LOCAL_PATH = re.compile(
    r"""(?P<quote>["'`])(?P<path>/(?:data|memory|logs|chats|mount|run)/.*?)(?P=quote)""",
    re.DOTALL,
)
_CSS_URL = re.compile(
    r"""url\(\s*(?:["']?)(?P<path>/(?:data|memory|logs|chats|mount|run)/[^)"']+)"""
)
_DYNAMIC_MARKERS = ("${", "{{", "<%", "' +", '" +', "` +")


class _ResourceAttributeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paths: list[str] = []

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name.lower() in {"src", "href", "poster"} and value:
                self.paths.append(value)
            elif name.lower() == "style" and value:
                self.paths.extend(match.group("path") for match in _CSS_URL.finditer(value))


class _MarkdownImageParser(HTMLParser):
    """Collect image sources from Python-Markdown's normalized HTML output."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paths: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "img":
            return
        for name, value in attrs:
            if name.lower() == "src" and value:
                self.paths.append(value)


def _rule_from_candidate(candidate: str) -> str | None:
    value = unquote(candidate.strip())
    if not value.startswith(_ROOTS):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    path = parsed.path
    if (
        not path.startswith(_ROOTS)
        or "\x00" in path
        or "\\" in path
        or any(segment in {".", ".."} for segment in path.split("/"))
    ):
        return None

    marker_positions = [
        position
        for marker in _DYNAMIC_MARKERS
        if (position := path.find(marker)) >= 0
    ]
    is_dynamic = bool(marker_positions)
    if marker_positions:
        path = path[:min(marker_positions)]
        # Dynamic expressions receive only the statically declared directory.
        if not path.endswith("/"):
            path = path.rsplit("/", 1)[0] + "/"

    is_prefix = path.endswith("/")
    normalized = posixpath.normpath(path)
    if not normalized.startswith(_ROOTS):
        return None
    # A dynamic expression such as ``/data/${userInput}`` does not declare a
    # meaningful least-privilege boundary. Granting the entire VFS root would
    # let an artifact turn its own string interpolation into a file oracle.
    # The Agent must place dynamic resources below a static subdirectory (for
    # example ``/data/images/${name}``).
    if is_dynamic and normalized in {"/data", "/memory", "/logs", "/chats", "/mount", "/run"}:
        return None
    if is_prefix:
        normalized += "/"
    return normalized


def html_vfs_read_rules(html: str, source_path: str | None = None) -> tuple[str, ...]:
    """Return exact files or static directory prefixes referenced by HTML.

    The Agent keeps using ordinary Linux paths; no capability vocabulary is
    added to the tool schema. Dynamic template strings are narrowed to their
    longest static directory prefix. File previews also resolve relative resource
    references against their source file, constrained to that file's VFS root.
    """
    parser = _ResourceAttributeParser()
    try:
        parser.feed(html)
    except Exception:
        # Quoted JS/CSS scanning below remains useful for malformed fragments.
        pass
    candidates = list(parser.paths)
    candidates.extend(
        match.group("path") for match in _QUOTED_LOCAL_PATH.finditer(html)
    )
    candidates.extend(match.group("path") for match in _CSS_URL.finditer(html))
    rules = {
        rule
        for candidate in candidates
        if (rule := _rule_from_candidate(candidate)) is not None
    }
    if source_path is not None:
        rules.update(
            rule
            for candidate in parser.paths
            if (rule := _relative_file_rule(candidate, source_path)) is not None
        )
    return tuple(sorted(rules))


def _relative_file_rule(candidate: str, source_path: str) -> str | None:
    value = unquote(candidate.strip())
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    path = parsed.path
    if "\x00" in path or "\\" in path or any(marker in path for marker in _DYNAMIC_MARKERS):
        return None
    if path.startswith("/"):
        return _rule_from_candidate(path)

    source = posixpath.normpath(source_path)
    source_parts = source.split("/")
    if len(source_parts) < 3 or f"/{source_parts[1]}/" not in _ROOTS:
        return None
    root = f"/{source_parts[1]}/"
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
    if not resolved.startswith(root) or resolved.endswith("/"):
        return None
    if path.endswith("/") or resolved == posixpath.dirname(source):
        return None
    return resolved


def markdown_vfs_read_rules(markdown: str, source_path: str) -> tuple[str, ...]:
    """Return exact VFS images referenced by one Markdown document.

    Markdown is normalized through the project's CommonMark-compatible parser
    so inline and reference-style image syntax share one policy path. Relative
    references may move between sibling directories, but never outside the
    source document's VFS root.
    """
    try:
        html = markdown_lib.markdown(markdown, extensions=["extra"])
    except Exception:
        return ()
    parser = _MarkdownImageParser()
    try:
        parser.feed(html)
    except Exception:
        return ()
    rules = {
        rule
        for candidate in parser.paths
        if (rule := _relative_file_rule(candidate, source_path)) is not None
    }
    return tuple(sorted(rules))


def diagram_vfs_read_rules(source: str | bytes) -> tuple[str, ...]:
    """Return exact VFS files referenced by Universal Scene Graph images.

    Diagram resources remain ordinary Agent-visible paths in the persisted
    document.  Preview turns only image-element references into short-lived
    capabilities; similarly named values in metadata or arbitrary JSON do not
    broaden the readable file set.
    """
    try:
        payload: Any = json.loads(source)
    except (TypeError, ValueError, UnicodeDecodeError):
        return ()
    if not isinstance(payload, dict):
        return ()

    candidate_graphs: list[Any] = []
    model = payload.get("model")
    if isinstance(model, dict):
        candidate_graphs.append(model.get("sceneGraph"))
    candidate_graphs.append(payload.get("graph"))

    rules: set[str] = set()
    for graph in candidate_graphs:
        if not isinstance(graph, dict):
            continue
        elements = graph.get("elements")
        if not isinstance(elements, list):
            continue
        for element in elements:
            if not isinstance(element, dict) or element.get("elementType") != "image":
                continue
            resource_ref = element.get("resourceRef")
            if not isinstance(resource_ref, str):
                continue
            rule = _rule_from_candidate(resource_ref)
            if rule is not None and not rule.endswith("/"):
                rules.add(rule)
    return tuple(sorted(rules))


def rules_for_root(rules: tuple[str, ...], root: str) -> tuple[str, ...]:
    prefix = "/" + root.strip("/") + "/"
    return tuple(rule for rule in rules if rule.startswith(prefix))
