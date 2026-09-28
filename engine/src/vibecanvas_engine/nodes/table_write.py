# -*- coding: utf-8 -*-
"""TableWriteNode — write structured data to a local file (CSV, JSONL, Excel)."""

import os
import re
import jsonschema
from copy import deepcopy

from ..utils import safe_call_with_args
from ..register import node_registry
from .base import BaseNode
from . import table_io


@node_registry.register()
class TableWriteNode(BaseNode):
    """Write structured data to a local CSV, JSONL, or Excel file."""

    # Blocking file I/O + needs ``extra`` (for run_dir) → thread bridge.
    REQUIRES_THREAD_BRIDGE = True

    CONFIG_SCHEMA = {
        "type": "object",
        "required": ["file_path", "write_mode"],
        "properties": {
            "file_path": {
                "type": "string",
                "pattern": table_io.FILE_PATH_PATTERN,
                "description": "Absolute Workflow-runtime output path under /run or /mount. Use /run for execution-local output and /mount only for intentionally persistent output. Never use the Agent authoring workspace /data. Supports {{field_name}} interpolation."
            },
            "data_write": {
                "type": "string",
                "description": "(Optional) The input field holding the data to write. Must be an OBJECT (→ one row) or a LIST of objects (→ one row each; the table schema is taken from the first item). If omitted, the node auto-detects the first list-of-objects input."
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
            "write_mode": {
                "type": "string",
                "enum": ["overwrite", "append"],
                "description": "Write mode. 'overwrite' replaces existing file. 'append' adds to end."
            }
        },
        "additionalProperties": False
    }

    AGENT_SPEC = {
        "summary": "Write structured rows to a local CSV, JSONL, or Excel file.",
        "when_to_use": "Use when the workflow needs to persist processed tabular data for downstream nodes or user inspection.",
        "when_not_to_use": "For reading files use TableReadNode. For sending data to APIs use HTTPRequestNode.",
        "constraints": [
            "Use node_type='TableWriteNode' and at most one child.",
            "file_path must be an absolute Workflow-runtime path under /run/... or /mount/...; /data, /memory, /logs, relative paths, and arbitrary OS paths are not available to Workflow nodes.",
            "Use /run for normal same-execution output. Use /mount only when the result intentionally needs to persist across runs or be shared with the user.",
            "file_path supports {{field_name}} interpolation, including a whole path such as {{csv_path}}. The resolved path must stay under /run/... or /mount/...; missing fields and path traversal fail before file I/O. Downstream readers can bind the file_path output directly.",
            "file_format defaults to auto and detects .csv, .json/.jsonl/.ndjson, or .xlsx from the path.",
            "data_write optionally names the input field to write: object writes one row; list of objects writes many rows using the first row's keys as columns.",
            "If data_write is omitted, the node uses the first list-of-objects input, otherwise combines all inputs into one row.",
            "append mode requires existing columns to match the new row columns; otherwise the write fails with a schema mismatch.",
            "output_fields must be exactly file_path (string) and rows_written (integer)."
        ],
        "config_guide": {
            "file_path": "Absolute output path under /run or /mount (.csv, .jsonl, .ndjson, .xlsx). Prefer /run for normal execution output; use /mount only for intentional persistence. Never use /data. Downstream readers must use the exact same path. Supports {{field_name}} interpolation.",
            "data_write": "(Optional) The input field holding the rows to write. Object → one row; list of objects → one row each (schema from the first item). Only object/list values are supported.",
            "file_format": "(Optional) Override format detection. Values: auto, csv, jsonl, excel. Default: auto.",
            "sheet_name": "(Optional) Sheet/tab name for Excel files.",
            "write_mode": "Write mode: 'overwrite' creates/replaces the file, 'append' adds data to the end of an existing file."
        },
        "examples": [
            {
                "scenario": "Write processing results to a JSONL file",
                "node_dict": {
                    "node_id": "node_5",
                    "node_name": "save_results",
                    "node_type": "TableWriteNode",
                    "node_description": "Save processed results to JSONL",
                    "input_fields": {
                        "rows": {"type": "array", "value": [], "reference": "processor.rows"}
                    },
                    "output_fields": {
                        "file_path": {"type": "string", "description": "Written file path"},
                        "rows_written": {"type": "integer", "description": "Number of rows written"}
                    },
                    "node_config": {
                        "file_path": "/run/results/output.jsonl",
                        "write_mode": "overwrite"
                    },
                    "children": ["node_6"],
                    "__attributes__": {"x": 400, "y": 0}
                }
            }
        ],
        "display": {
            "name": {"en": "TableWriteNode", "zh": "表格写入节点"},
            "description": {"en": "Write data to a local CSV, JSONL, or Excel file", "zh": "将数据写入本地 CSV、JSONL 或 Excel 文件"},
            "icon": "file_write",
            "category": {"en": "Data I/O", "zh": "数据读写"},
        }
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    @staticmethod
    @safe_call_with_args(prefix="[TableWriteNode Check]: ")
    def check(node_dict: dict) -> bool:
        jsonschema.validate(instance=node_dict, schema=BaseNode.GENERAL_NODE_SCHEMA)

        specific_schema = deepcopy(BaseNode.GENERAL_NODE_SCHEMA)
        specific_schema["properties"]["node_config"] = TableWriteNode.CONFIG_SCHEMA
        jsonschema.validate(instance=node_dict, schema=specific_schema)

        jsonschema.validate(instance=node_dict, schema={
            "type": "object",
            "properties": {
                "node_type": {"const": "TableWriteNode"},
                "children": {"type": "array", "maxItems": 1}
            }
        })

        output_fields = node_dict.get("output_fields", {})
        assert set(output_fields.keys()) == {"file_path", "rows_written"}, (
            "For TableWriteNode, output_fields must be exactly 'file_path' "
            "and 'rows_written'."
        )
        assert output_fields["file_path"].get("type") == "string", (
            "For TableWriteNode, output_fields.file_path type must be 'string'."
        )
        assert output_fields["rows_written"].get("type") == "integer", (
            "For TableWriteNode, output_fields.rows_written type must be 'integer'."
        )

        data_write = node_dict["node_config"].get("data_write")
        if data_write:
            assert data_write in node_dict.get("input_fields", {}), (
                f"For TableWriteNode, data_write field '{data_write}' must "
                "exist in input_fields."
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

    @staticmethod
    def _resolve_rows(config: dict, inputs: dict) -> list:
        """The list of row-dicts to write.

        With ``data_write`` set: that input field is the data source — an OBJECT
        becomes one row; a LIST becomes one row per item (every item must be an
        object). Without it, fall back to auto-detect (first list-of-objects
        input, else all inputs combined as a single row). Raises a clear error
        for a missing field or an unsupported value type.
        """
        data_field = config.get("data_write")
        if data_field:
            if data_field not in inputs:
                raise ValueError(
                    f"data_write field '{data_field}' is not among this node's inputs."
                )
            data = inputs[data_field]
            if isinstance(data, dict):
                return [data]
            if isinstance(data, list):
                if any(not isinstance(it, dict) for it in data):
                    raise ValueError(
                        f"data_write field '{data_field}' is a list but not every item "
                        f"is an object (table row)."
                    )
                return data
            raise ValueError(
                f"data_write field '{data_field}' must be an object or a list of "
                f"objects, got {type(data).__name__}. Only object/list data can be "
                f"written as a table."
            )
        # Back-compat auto-detect: first list-of-dicts input, else one combined row.
        for v in inputs.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        return [dict(inputs)]

    @safe_call_with_args(prefix="[TableWriteNode Call]: ")
    def __call__(self, inputs: dict, previous_outputs: dict, extra: dict = None) -> dict:
        config = self.node_config

        # Interpolate {{fields}}. The result is already a real path inside the
        # sandbox, so it is written directly. Workflow runtime exposes `/run`
        # and `/mount`; use `/mount` only for intentional cross-run files.
        file_path = table_io.resolve_file_path(config["file_path"], inputs)

        fmt = config.get("file_format", "auto")
        if fmt == "auto":
            fmt = table_io.detect_format(file_path)
        sheet_name = self._interpolate(config.get("sheet_name", ""), inputs)
        write_mode = config.get("write_mode", "overwrite")

        rows = self._resolve_rows(config, inputs)
        # The table schema comes from the first row.
        headers = list(rows[0].keys()) if rows else []

        # Append-mode boundary: when the target file already exists, its columns
        # must correspond to the data's columns — otherwise the writer would
        # silently drop/misalign fields (DictWriter extrasaction='ignore').
        if write_mode == "append" and rows and os.path.exists(file_path):
            existing_headers = table_io.read_headers(file_path, fmt, sheet_name or None)
            if set(existing_headers) != set(headers):
                raise ValueError(
                    f"Append schema mismatch for '{file_path}': existing columns "
                    f"{existing_headers} do not match the data columns {headers}. "
                    f"Use write_mode 'overwrite', or align the fields."
                )

        table_io.write_rows(
            file_path, fmt, rows, headers,
            sheet_name=sheet_name or None,
            append=(write_mode == "append"),
        )

        return {
            "file_path": file_path,
            "rows_written": len(rows),
        }
