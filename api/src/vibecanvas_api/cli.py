"""argparse CLI for vibecanvas-api.

Subcommands:
    serve         — run uvicorn against vibecanvas_api.app:build_app()
    dump-openapi  — write openapi.json to disk (no uvicorn)

"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    # Keep uvicorn from replacing the application's structlog configuration.
    # This ensures access and error logs use the same structured JSON pipeline.
    uvicorn.run(
        "vibecanvas_api.app:build_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
        log_config=None,
    )
    return 0


def cmd_dump_openapi(args: argparse.Namespace) -> int:
    from .app import build_app

    app = build_app()
    spec = app.openapi()
    out = Path(args.output)
    out.write_text(json.dumps(spec, indent=2, ensure_ascii=False))
    print(f"openapi spec ({len(spec.get('paths', {}))} paths) written to {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vibecanvas-api",
        description="Flowork data plane HTTP service.",
    )
    p.add_argument("--version", action="version",
                   version=f"vibecanvas-api {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="Run the FastAPI server via uvicorn.")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--reload", action="store_true",
                   help="Auto-reload on source changes (dev only).")
    s.add_argument("--log-level", default="info",
                   choices=["critical", "error", "warning", "info",
                            "debug", "trace"])
    s.set_defaults(func=cmd_serve)

    d = sub.add_parser("dump-openapi", help="Dump openapi.json to disk.")
    d.add_argument("--output", default="openapi.json")
    d.set_defaults(func=cmd_dump_openapi)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
