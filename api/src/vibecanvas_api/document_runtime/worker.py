"""Private worker protocol consumed by the runtime, not by shell evidence parsing."""

import json
from pathlib import Path
import sys
import signal

from .rendering import render_document
from .review import review_document


def main():
    def terminate(_signum, _frame):
        raise SystemExit(1)

    signal.signal(signal.SIGTERM, terminate)
    def emit(value):
        print(json.dumps(value, ensure_ascii=False), flush=True)

    try:
        request = json.load(sys.stdin)
        arguments = request["arguments"]
        if request["operation"].startswith("diagram."):
            from vibecanvas_api.diagram_runtime.operations import review_diagram, render_diagram, search_shapes
            operation = request["operation"]
            if operation == "diagram.review":
                result = review_diagram(**arguments)
            elif operation == "diagram.render":
                result = render_diagram(**arguments, progress=lambda value: emit({"_progress": value}))
            elif operation == "diagram.search-shapes":
                result = search_shapes(**arguments, progress=lambda value: emit({"_progress": value}))
            else:
                raise ValueError("Unsupported diagram operation.")
        elif request["operation"] == "document.review":
            result = review_document(arguments["file"])
            result["file"] = str(Path(result.pop("path")).resolve())
            result["status"] = "passed" if result.pop("valid") else "failed"
            result["message"] = (
                "Structural checks passed. Factual accuracy, formula meaning and visual quality are not validated."
                if result["status"] == "passed"
                else "Structural checks failed. Fix the reported errors before delivery."
            )
            result["hint"] = (
                "Verify the content against the user's requirements and independently check calculations. "
                + ("Check that metric labels, units and comparison periods match the calculations, "
                   "including summaries and charts. Missing comparison data is unavailable, not zero. "
                   if result.get("format") == "xlsx" else "")
                + "For DOCX/PPTX/XLSX/PDF, render all pages and inspect every image before render_preview. "
                "After changes, review and render the exact final revision again."
                if result["status"] == "passed"
                else "Correct the errors, then rerun document review --file on the updated source."
            )
        elif request["operation"] == "document.render":
            result = render_document(arguments["file"], **{key: value for key, value in arguments.items() if key != "file"}, progress=lambda value: emit({"_progress": value}))
        else:
            raise ValueError("Unsupported document operation.")
    except Exception as exc:
        resource = "diagram" if str(locals().get("request", {}).get("operation", "")).startswith("diagram.") else "document"
        result = {"status": "failed", "error": resource + "_failed", "message": str(exc), "hint": f"Check the source file/runtime and fix the reported issue. See flowork-cli {resource} <command> --help."}
    emit(result)


if __name__ == "__main__":
    main()
