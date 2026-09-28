# -*- coding: utf-8 -*-
"""TableReadNode — read structured data from a local file (CSV, JSONL, Excel)."""

import re
import jsonschema
from copy import deepcopy

from ..utils import safe_call_with_args
from ..register import node_registry
from .base import BaseNode
from . import table_io


@node_registry.register()
class TableReadNode(BaseNode):
    """Read structured data from a local CSV, JSONL, or Excel file."""

    # Blocking file I/O + needs ``extra`` (for run_dir) → run off the event loop
    # via the thread bridge (nodes/exec.py), like PromptNode/HTTPRequestNode.
    REQUIRES_THREAD_BRIDGE = True

    CONFIG_SCHEMA = {
        "type": "object",
        "required": ["file_path"],
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["batch", "stream"],
                "description": "Read mode. 'batch' loads rows into memory. 'stream' (reserved, not yet implemented) for lazy iteration."
            },
            "file_path": {
                "type": "string",
                "pattern": table_io.FILE_PATH_PATTERN,
                "description": "Absolute Workflow-runtime path under /run or /mount. /run is execution-local; /mount is user-persistent. The file must already exist or be written by an upstream node. Never use the Agent authoring workspace /data. Supports {{field_name}} interpolation."
            },
            "file_format": {
                "type": "string",
                "enum": ["auto", "csv", "jsonl", "excel"],
                "description": "(Optional) File format. Default 'auto' detects from the file extension."
            },
            "sheet_name": {
                "type": "string",
                "description": "(Optional) Sheet name for Excel files."
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "(Optional) Skip first N data rows. Default 0."
            },
            "limit": {
                "type": "integer",
                "minimum": 0,
                "description": "(Optional) Max rows to read. 0 = read all. Default 0."
            }
        },
        "additionalProperties": False
    }

    AGENT_SPEC = {
        "summary": "Read CSV, JSONL, or Excel data from a local file into rows, headers, row_count, and an inferred row schema.",
        "when_to_use": "Use when the workflow needs tabular file data as structured rows for later processing.",
        "when_not_to_use": "For calling external APIs use HTTPRequestNode. For writing data to files use TableWriteNode.",
        "constraints": [
            "Use node_type='TableReadNode' and at most one child.",
            "file_path must be an absolute Workflow-runtime path under /run/... or /mount/...; /data, /memory, /logs, relative paths, and arbitrary OS paths are not available to Workflow nodes.",
            "Never invent a file path or assume a file exists. Read only a user-provided existing /mount/... file or the exact /run/... or /mount/... path written by an upstream node.",
            "Use /run for files produced and consumed in the same execution. Use /mount only for user-provided or intentionally cross-run persistent files.",
            "file_path supports {{field_name}} interpolation, including a whole path such as {{csv_path}} bound to an upstream file_path output. The resolved path must stay under /run/... or /mount/...; missing fields and path traversal fail before file I/O.",
            "Use batch mode. stream mode is reserved and will fail at runtime.",
            "file_format defaults to auto and detects .csv, .json/.jsonl/.ndjson, or .xlsx from the path; set it explicitly when the extension is ambiguous.",
            "sheet_name is only for Excel files.",
            "offset and limit return a row slice after the file is loaded; limit=0 means all rows.",
            "output_fields must be exactly rows (array), headers (array), row_count (integer), and schema (object)."
        ],
        "config_guide": {
            "file_path": "Known existing absolute path under /run or /mount. Use /run for a same-execution upstream output and /mount for a user-provided persistent file. Never use /data and never assume an unproduced file exists. Supports {{field_name}} interpolation.",
            "file_format": "(Optional) Override format detection. Values: auto, csv, jsonl, excel. Default: auto.",
            "sheet_name": "(Optional) Sheet/tab name for Excel files. Ignored for CSV/JSONL.",
            "offset": "(Optional) Skip first N data rows. Default: 0.",
            "limit": "(Optional) Maximum number of rows to read. 0 means read all. Default: 0."
        },
        "examples": [
            {
                "scenario": "Read a user-provided mounted CSV with pagination",
                "node_dict": {
                    "node_id": "node_3",
                    "node_name": "read_data",
                    "node_type": "TableReadNode",
                    "node_description": "Read first 100 rows from a CSV file",
                    "input_fields": {},
                    "output_fields": {
                        "rows": {"type": "array", "description": "Data rows"},
                        "headers": {"type": "array", "description": "Column names"},
                        "row_count": {"type": "integer", "description": "Number of rows read"},
                        "schema": {"type": "object", "description": "Inferred JSON Schema"}
                    },
                    "node_config": {
                        "file_path": "/mount/uploads/input.csv",
                        "file_format": "auto",
                        "offset": 0,
                        "limit": 100
                    },
                    "children": ["node_4"],
                    "__attributes__": {"x": 200, "y": 0}
                }
            }
        ],
        "display": {
            "name": {"en": "TableReadNode", "zh": "表格读取节点"},
            "description": {"en": "Read data from a local CSV, JSONL, or Excel file", "zh": "从本地 CSV、JSONL 或 Excel 文件读取数据"},
            "icon": "file_read",
            "category": {"en": "Data I/O", "zh": "数据读写"},
        }
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    @staticmethod
    @safe_call_with_args(prefix="[TableReadNode Check]: ")
    def check(node_dict: dict) -> bool:
        jsonschema.validate(instance=node_dict, schema=BaseNode.GENERAL_NODE_SCHEMA)

        specific_schema = deepcopy(BaseNode.GENERAL_NODE_SCHEMA)
        specific_schema["properties"]["node_config"] = TableReadNode.CONFIG_SCHEMA
        jsonschema.validate(instance=node_dict, schema=specific_schema)

        jsonschema.validate(instance=node_dict, schema={
            "type": "object",
            "properties": {
                "node_type": {"const": "TableReadNode"},
                "children": {"type": "array", "maxItems": 1}
            }
        })

        output_fields = node_dict.get("output_fields", {})
        expected_outputs = {"rows", "headers", "row_count", "schema"}
        assert set(output_fields.keys()) == expected_outputs, (
            "For TableReadNode, output_fields must be exactly 'rows', "
            "'headers', 'row_count', and 'schema'."
        )
        assert output_fields["rows"].get("type") == "array", (
            "For TableReadNode, output_fields.rows type must be 'array'."
        )
        assert output_fields["headers"].get("type") == "array", (
            "For TableReadNode, output_fields.headers type must be 'array'."
        )
        assert output_fields["row_count"].get("type") == "integer", (
            "For TableReadNode, output_fields.row_count type must be 'integer'."
        )
        assert output_fields["schema"].get("type") == "object", (
            "For TableReadNode, output_fields.schema type must be 'object'."
        )

    @staticmethod
    def _interpolate(template_str: str, inputs: dict) -> str:
        def _replace(match):
            key = match.group(1).strip()
            if key in inputs:
                val = inputs[key]
                return str(val) if not isinstance(val, (dict, list)) else str(val)
            return match.group(0)
        return re.sub(r"\{\{(.*?)\}\}", _replace, str(template_str))

    @safe_call_with_args(prefix="[TableReadNode Call]: ")
    def __call__(self, inputs: dict, previous_outputs: dict, extra: dict = None) -> dict:
        config = self.node_config
        mode = config.get("mode", "batch")
        if mode == "stream":
            raise NotImplementedError("Stream mode is reserved for future implementation. Use 'batch' mode with offset/limit for large files.")

        # Interpolate {{fields}}. The result is already a real path inside the
        # sandbox, so it is opened directly. Workflow runtime exposes `/run`
        # and `/mount`; Agent authoring roots such as `/data` are not available.
        file_path = table_io.resolve_file_path(config["file_path"], inputs)

        fmt = config.get("file_format", "auto")
        if fmt == "auto":
            fmt = table_io.detect_format(file_path)
        sheet_name = self._interpolate(config.get("sheet_name", ""), inputs)
        offset = int(config.get("offset", 0))
        limit = int(config.get("limit", 0))

        all_rows = table_io.read_rows(file_path, fmt, sheet_name or None)

        rows = all_rows[offset:]
        if limit > 0:
            rows = rows[:limit]

        headers = list(rows[0].keys()) if rows else []
        schema = table_io.infer_schema(rows)

        return {
            "rows": rows,
            "headers": headers,
            "row_count": len(rows),
            "schema": schema,
        }
