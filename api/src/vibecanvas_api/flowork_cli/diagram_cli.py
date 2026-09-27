"""Agent-facing stateless diagram commands; no MCP or connection state."""

import json
import os

try:
    from . import document_cli
except ImportError:
    import document_cli

OPERATIONS = frozenset({"diagram.search-shapes", "diagram.review", "diagram.render"})

XML_EXAMPLE = '''Native uncompressed XML example:
<mxfile><diagram id="page-1" name="Overview"><mxGraphModel><root>
  <mxCell id="0"/><mxCell id="1" parent="0"/>
  <mxCell id="a" value="Start" style="rounded=1;whiteSpace=wrap;html=1;" vertex="1" parent="1">
    <mxGeometry x="40" y="40" width="120" height="60" as="geometry"/>
  </mxCell>
</root></mxGraphModel></diagram></mxfile>
Keep IDs stable and unique within each page. XML-escape labels/style attributes
(e.g. &amp; and &quot;), preferably with a Python XML library. Edges use source and
target cell IDs in the same page, parent="1", edge="1", and an mxGeometry with
relative="1" as="geometry". Plain geometry labels have no HTML formatting unless
html=1 is used. Do not create a second JSON graph format or use connection state.'''


def add_parser(groups):
    import argparse
    group = groups.add_parser("diagram", help="Search shapes, review or render native .drawio files.",
        description="Edit native uncompressed .drawio XML with ordinary file tools. CLI commands do not modify the source or implicitly rearrange nodes/edges. No open/save/connect commands; publish accepted files with render_preview MCP.")
    actions = group.add_subparsers(dest="action", required=True)
    search = actions.add_parser("search-shapes", help="Find official draw.io styles for specialized shapes/icons.",
        description="Uses the official draw.io search algorithm and public shape index (cached locally); optional icon service supplements sparse results. Network/index failures are reported, never silently treated as no matches. Results contain title, style, width, height. Use the exact style in XML; escape it as an XML attribute. Remote image styles may require network when rendered. An empty shapes list is valid, not a tool failure. Basic rectangles/diamonds need no search. Output {status,query,shapes,warnings,message}; no document is changed.",
        epilog='Example: flowork-cli diagram search-shapes --query "aws s3" --limit 5')
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=10, help="1–50 results; default 10.")
    review = actions.add_parser("review", help="Check native draw.io structure without editing it.",
        description="Checks XML safety, page/model structure, page-local IDs including object wrappers, parent/edge references, parent cycles and geometry numbers. Repeated root/layer IDs across DIFFERENT pages are valid. Reads both compressed and uncompressed pages; create formatted uncompressed XML for easy incremental edits. Existing Preview/parser safety limits also apply. Output {status:passed|failed,file,format,source_hash,errors,warnings,details:{pages:[...]}}; issues identify page/cell where possible. Warnings alone exit 0; errors exit 1. Not a visual-quality or semantic-correctness guarantee. Inspect rendered pixels after review.",
        epilog=XML_EXAMPLE, formatter_class=argparse.RawDescriptionHelpFormatter)
    review.add_argument("--file", required=True, help="Existing .drawio file, absolute or relative to your shell working directory.")
    render = actions.add_parser("render", help="Render all or selected draw.io pages through official Desktop.",
        description="Uses the pinned official draw.io Desktop renderer, not a replacement renderer. Default: ALL pages. Works from a source snapshot without modifying layout or rewriting your file. Output incremental JSONL page progress, then {status,file,source_hash,total_pages,rendered_pages,complete,output_dir,images:[{page,name,file}],message}. No image/base64 stdout. On failure completed images remain and exit is nonzero. No fixed total execution deadline. Open every PNG with view_image before delivery. Partial --pages coverage is not full-document acceptance; coverage accumulates only for the same file/source_hash. Editing source invalidates evidence. Publish the final .drawio via render_preview, not the feedback PNG.",
        epilog='Examples: flowork-cli diagram render --file /data/system.drawio; flowork-cli diagram render --file system.drawio --pages "1-3,5" --output_dir /data/diagram-review')
    render.add_argument("--file", required=True)
    render.add_argument("--pages", help="1-based page positions, e.g. 1-3,5; not page IDs/names. Omit for all.")
    render.add_argument("--output_dir", help="CLI creates this NEW directory; do NOT mkdir it first. Only its parent must exist. Reusing an existing directory fails without overwriting files. Prefer omitting this option for a unique /memory/diagram-feedback directory.")


def validate(operation, arguments):
    if operation not in OPERATIONS:
        raise ValueError("Unsupported diagram operation.")
    if operation == "diagram.search-shapes":
        if set(arguments) - {"query", "limit"}:
            raise ValueError("Unsupported shape-search arguments.")
        query, limit = arguments.get("query"), arguments.get("limit", 10)
        if not isinstance(query, str) or not query.strip() or "\x00" in query:
            raise ValueError("--query must contain non-empty search keywords.")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("--limit must be between 1 and 50.")
        return {"query": query.strip(), "limit": limit}
    if "dpi" in arguments:
        raise ValueError("Diagram rendering uses native canvas dimensions, not --dpi.")
    document_cli.validate(operation.replace("diagram.", "document."), arguments)
    if not arguments["file"].lower().endswith(".drawio"):
        raise ValueError("--file must name a native .drawio file.")
    return dict(arguments)


def execute(args, endpoint, core):
    operation = "diagram." + args.action
    if args.action == "search-shapes":
        arguments = {"query": args.query, "limit": args.limit}
    else:
        arguments = {"file": os.path.abspath(args.file)}
        if args.action == "render":
            if args.pages is not None:
                arguments["pages"] = args.pages
            if args.output_dir is not None:
                arguments["output_dir"] = os.path.abspath(args.output_dir)
    arguments = core.validate_arguments(operation, arguments)
    if not endpoint:
        result = core.error("runtime_unavailable", "No active Agent runtime is available.", "Run during an active cloud Agent turn.")
    else:
        try:
            result = core.request(endpoint, arguments, operation=operation)
        except (OSError, ValueError):
            result = core.error("runtime_unavailable", "Diagram execution was interrupted or its result is unavailable.", "Inspect any completed output files, then retry in an active turn. Do not claim review passed.")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 2 if result.get("error") == "invalid_arguments" else (0 if result.get("status") in {"passed", "succeeded"} else 1)
