"""Stdlib-only Document CLI; execution and evidence belong to the runtime."""

import json
import os
import re

OPERATIONS = frozenset({"document.review", "document.render"})


def add_parser(groups):
    group = groups.add_parser("document", help="Review or render sandbox documents without changing the source.")
    actions = group.add_subparsers(dest="action", required=True)
    review = actions.add_parser("review", help="Check deterministic document structure.",
        description="Supports DOCX/PPTX/XLSX/PDF/Markdown/HTML/CSV/TSV/SVG/TXT. Read-only; not a factual, visual or formula-correctness guarantee. Output status passed/failed, file, format, source_hash, errors, warnings and details. Warnings alone exit 0; failed checks exit 1. After editing the source, review again.",
        epilog="Example: flowork-cli document review --file /data/report.docx")
    review.add_argument("--file", required=True, help="Existing sandbox file; absolute or relative to your shell directory.")
    render = actions.add_parser("render", help="Render all or selected document pages as PNGs.",
        description="Supports DOCX/PPTX/XLSX/PDF and render-only ODT/ODP/ODS. Default: ALL pages, no page-count truncation. XLSX page numbers refer to print-layout pages, not sheet indices. Never edits or recalculates the source in place. Prints incremental JSONL page progress, then a final result with source_hash, total_pages, rendered_pages, complete, output_dir and images [{page,file}]. Partial failures retain completed images and exit 1. No fixed execution deadline. Open every PNG with the native image tool; rendering alone is not visual review. Only coverage of every page of the SAME source_hash can satisfy full-document delivery. Editing the source invalidates prior evidence. Use render_preview MCP separately to publish the final native file.",
        epilog='Examples: flowork-cli document render --file /data/report.pdf; flowork-cli document render --file report.pdf --pages "1-3,5" --dpi 144 --output_dir /data/review-pages')
    render.add_argument("--file", required=True)
    render.add_argument("--pages", help="1-based inclusive ranges/comma-separated pages, e.g. 1-3,5. Omit for all pages.")
    render.add_argument("--dpi", type=int, default=144, help="96–220; default 144.")
    render.add_argument("--output_dir", help="Prefer omitting this option: the CLI creates a unique /memory/document-feedback directory. If specified, the CLI creates this NEW directory; do not mkdir it first. Only its parent must exist. Existing directories are rejected without overwriting.")


def validate(operation, arguments):
    allowed = {"file"} if operation == "document.review" else {"file", "pages", "dpi", "output_dir"}
    if operation not in OPERATIONS or set(arguments) - allowed:
        raise ValueError("Unsupported document operation or arguments.")
    for key in ("file", "output_dir"):
        value = arguments.get(key)
        if key == "file" or value is not None:
            if not isinstance(value, str) or not value.strip() or "\x00" in value or not os.path.isabs(value):
                raise ValueError(f"{key} must identify an absolute sandbox path.")
    if operation == "document.render":
        dpi = arguments.get("dpi", 144)
        if type(dpi) is not int or not 96 <= dpi <= 220:
            raise ValueError("--dpi must be between 96 and 220.")
        pages = arguments.get("pages")
        if pages is not None and (not isinstance(pages, str) or not re.fullmatch(r"[1-9]\d*(?:-[1-9]\d*)?(?:,[1-9]\d*(?:-[1-9]\d*)?)*", pages)):
            raise ValueError("--pages must contain 1-based page numbers/ranges, for example 1-3,5.")
        for part in (pages or "").split(","):
            if "-" in part and int(part.split("-")[0]) > int(part.split("-")[1]):
                raise ValueError("Page range start must not exceed its end.")
    return dict(arguments)


def execute(args, endpoint, core):
    operation = "document." + args.action
    arguments = {"file": os.path.abspath(args.file)}
    if args.action == "render":
        arguments["dpi"] = args.dpi
        if args.pages is not None:
            arguments["pages"] = args.pages
        if args.output_dir is not None:
            arguments["output_dir"] = os.path.abspath(args.output_dir)
    arguments = core.validate_arguments(operation, arguments)
    if not endpoint:
        result = core.error("runtime_unavailable", "No active Agent runtime is available.", "Run this command during an active cloud Agent turn.")
    else:
        try:
            result = core.request(endpoint, arguments, operation=operation)
        except (OSError, ValueError):
            result = core.error("runtime_unavailable", "Document execution was interrupted or its result is unavailable.", "Inspect output files, then retry in an active Agent turn; do not claim review passed.")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 2 if result.get("error") == "invalid_arguments" else (0 if result.get("status") in {"passed", "succeeded"} else 1)
