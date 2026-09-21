"""Batch-result output destinations + format serialization.

The batch runner (``background_tasks/batch_exec.py``) aggregates every row into a
normalized results table (columns ``i / input / output / error``). TWO things are
abstracted here so new behavior plugs in without touching the task:

1. WHERE it's written — :class:`BatchOutputSink` (v1: :class:`VfsDataOutputSink`,
   the workflow's durable ``/data`` VFS; future: cloud sheets, buckets).
2. HOW it's serialized — :func:`serialize_results` picks the file FORMAT from the
   output path's extension (csv / tsv / jsonl / xlsx), so the user controls the
   format just by naming the file. Excel output additionally takes a sheet name.

The sink fully owns "write these rows to this destination in the right format":
the task hands it the structured rows and gets back the written location. The
Postgres/object-store write runs inside ``run_in_short_session`` (the background worker
worker is sync), tenant-scoped via the ``current_sync_tenant_id`` CV the task
sets at its top.
"""
from __future__ import annotations

import csv
import io
import json
import re
from abc import ABC, abstractmethod
from typing import Optional

from vibecanvas_api.services.object_store import get_object_store
from vibecanvas_api.storage.sync_session import run_in_short_session
from vibecanvas_api.storage.vfs_store import VfsRepo

# The only user-writable durable prefix batch output targets in v1 (mirrors the
# writable VFS allowlist — batch output is agent working data, so it uses /data.
_DATA_PREFIX = "/data/"

# Normalized legacy results columns — every format serializes these in this
# order when the client does not provide an explicit projection. `output` is the
# FULL per-node outputs map (intermediate + end nodes); `error` is the engine
# error summary; `execution_time` is wall-clock seconds for the row.
_RESULT_COLUMNS = [
    "index",
    "status",
    "attempt",
    "input",
    "output",
    "error",
    "execution_time",
]

_CT_CSV = "table/csv"
_CT_TSV = "table/tsv"
_CT_JSONL = "table/jsonl"
_CT_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Excel sheet-name rules: ≤31 chars, none of []:*?/\.
_EXCEL_FORBIDDEN = re.compile(r"[\[\]:*?/\\]")


def _ext(path: str) -> str:
    """Lowercased extension (no dot) of a path, '' when none."""
    base = path.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def _result_table(out_rows: list[dict]) -> tuple[list[str], list[list[str]]]:
    """Flatten the per-row dicts into a string table (input/output → JSON cells)."""
    rows: list[list[str]] = []
    for r in out_rows:
        et = r.get("execution_time")
        error = r.get("error", "") or ""
        if isinstance(error, dict):
            error = error.get("message") or json.dumps(error, ensure_ascii=False)
        rows.append([
            str(r.get("index", r.get("i", ""))),
            str(r.get("status") or ("success" if r.get("ok") else "error")),
            str(r.get("attempt", "")),
            json.dumps(r.get("input"), ensure_ascii=False),
            json.dumps(r.get("output"), ensure_ascii=False) if r.get("output") is not None else "",
            str(error),
            "" if et is None else f"{float(et):.4f}",
        ])
    return list(_RESULT_COLUMNS), rows


def _stringify_cell(value: object) -> str:
    """Render a resolved field value as a table cell.

    dict/list → JSON (ensure_ascii=False, so non-ASCII stays readable); anything
    else → ``str(value)``.
    """
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _project_field(row: dict, col: dict) -> str:
    """Resolve one ``field`` column from a row's ``output`` map, gracefully.

    ``output[col['node']][col['field']]``. Different rows may take different
    branches, so the node/field can be absent — when missing (node absent, node
    output not a dict, or field absent) fall back to a non-empty ``default`` if
    given, else ``""``. NEVER raises.
    """
    output = row.get("output")
    node = col.get("node")
    field = col.get("field")
    default = col.get("default")
    fallback = default if (default is not None and default != "") else ""
    if not isinstance(output, dict):
        return fallback
    node_out = output.get(node)
    if not isinstance(node_out, dict) or field not in node_out:
        return fallback
    return _stringify_cell(node_out[field])


def project_rows(
    out_rows: list[dict], columns: list[dict],
) -> tuple[list[str], list[list[str]]]:
    """Project rows onto a user-defined column schema → ``(headers, rows)``.

    ``columns`` is an ordered list of column specs; the output table is EXACTLY
    these columns in order. Supported ``kind``s:

    * ``index`` → the row's ``i``.
    * ``status`` → explicit row status, or derived from ``ok``/``error``.
    * ``execution_time`` → ``"" if None else f"{t:.4f}"``.
    * ``error`` → the row's ``error`` string.
    * ``field`` → ``output[node][field]`` with graceful fallback (see
      :func:`_project_field`).

    Lenient by design: an unrecognized/malformed column kind yields an empty
    cell rather than raising — validation happens elsewhere, never here.
    """
    headers = [str(c.get("name", "")) if isinstance(c, dict) else "" for c in columns]
    rows: list[list[str]] = []
    for r in out_rows:
        cells: list[str] = []
        for col in columns:
            if not isinstance(col, dict):
                cells.append("")
                continue
            kind = col.get("kind")
            if kind == "index":
                cells.append(str(r.get("i", "")))
            elif kind == "status":
                status = r.get("status")
                if status is None:
                    status = "success" if r.get("ok") else "error"
                cells.append(str(status))
            elif kind == "execution_time":
                et = r.get("execution_time")
                cells.append("" if et is None else f"{float(et):.4f}")
            elif kind == "error":
                cells.append(str(r.get("error", "") or ""))
            elif kind == "field":
                cells.append(_project_field(r, col))
            else:
                cells.append("")
        rows.append(cells)
    return headers, rows


def sanitize_sheet_name(name: Optional[str]) -> str:
    """Coerce a user sheet name to Excel's constraints (default ``Sheet1``)."""
    n = _EXCEL_FORBIDDEN.sub("_", (name or "").strip())
    return n[:31] or "Sheet1"


def serialize_results(
    out_rows: list[dict],
    *,
    path: str,
    sheet_name: Optional[str] = None,
    columns: Optional[list[dict]] = None,
) -> tuple[bytes, str]:
    """Serialize the results table in the FORMAT implied by ``path``'s extension.

    csv (default) / tsv / jsonl / xlsx|xls. Returns ``(bytes, content_type)``.
    Excel uses pandas + openpyxl and writes into ``sheet_name`` (sanitized).

    When ``columns`` (a user-defined schema) is given, the table is EXACTLY those
    columns in order, projected via :func:`project_rows`; otherwise the legacy
    fixed ``_RESULT_COLUMNS`` dump is used. The format dispatch below operates on
    the resulting ``(headers, rows)`` regardless.
    """
    if columns:
        headers, rows = project_rows(out_rows, columns)
    else:
        headers, rows = _result_table(out_rows)
    ext = _ext(path)

    if ext == "jsonl":
        lines = [json.dumps(dict(zip(headers, r)), ensure_ascii=False) for r in rows]
        return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8"), _CT_JSONL

    if ext in ("xlsx", "xls"):
        import pandas as pd  # local: heavy import, only on the Excel path

        df = pd.DataFrame(rows, columns=headers)
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as xw:
            df.to_excel(xw, index=False, sheet_name=sanitize_sheet_name(sheet_name))
        return buf.getvalue(), _CT_XLSX

    delim = "\t" if ext == "tsv" else ","
    sbuf = io.StringIO()
    w = csv.writer(sbuf, delimiter=delim)
    w.writerow(headers)
    for r in rows:
        w.writerow(r)
    return sbuf.getvalue().encode("utf-8"), (_CT_TSV if ext == "tsv" else _CT_CSV)


class BatchOutputSink(ABC):
    """A destination the batch runner writes its aggregated results table to."""

    @abstractmethod
    def write_rows(self, out_rows: list[dict]) -> str:
        """Serialize ``out_rows`` to this destination; return its location."""
        raise NotImplementedError


class VfsDataOutputSink(BatchOutputSink):
    """Write the results into the workflow's durable VFS under ``/data``.

    The path is wf-scoped + tenant-scoped (the same durable artifact store the
    Explorer's Agent-sandbox ``/data`` folder shows) and its extension selects
    the file format; ``sheet_name`` applies when that format is Excel.
    """

    def __init__(
        self,
        *,
        wf_id: str,
        tenant_id: str,
        path: str,
        sheet_name: Optional[str] = None,
        columns: Optional[list[dict]] = None,
    ):
        self._wf_id = wf_id
        self._tenant_id = tenant_id
        self._path = path
        self._sheet_name = sheet_name
        self._columns = columns

    @property
    def path(self) -> str:
        return self._path

    @property
    def sheet_name(self) -> Optional[str]:
        return self._sheet_name

    @property
    def columns(self) -> Optional[list[dict]]:
        return self._columns

    def write_rows(self, out_rows: list[dict]) -> str:
        data, content_type = serialize_results(
            out_rows, path=self._path, sheet_name=self._sheet_name,
            columns=self._columns,
        )

        async def _runner(session) -> None:
            repo = VfsRepo(session, object_store=get_object_store())
            await repo.upsert_artifact_bytes(
                wf_id=self._wf_id,
                tenant=self._tenant_id,
                path=self._path,
                data=data,
                content_type=content_type,
                abstract="Batch run results",
            )

        run_in_short_session(_runner)
        return self._path


def normalize_data_output_path(raw: str, *, default_name: str) -> str:
    """Validate + normalize a user-supplied ``/data`` output path.

    Rules: must resolve under ``/data``; no ``..`` traversal or control chars; a
    bare directory (``/data`` or a trailing ``/``) gets ``default_name`` appended.
    Raises ``ValueError`` on anything outside ``/data``.
    """
    p = (raw or "").strip()
    if not p:
        raise ValueError("output path is empty")
    if not p.startswith("/"):
        p = "/" + p
    if any(ord(c) < 0x20 for c in p) or ".." in p:
        raise ValueError("output path contains an invalid segment")
    if p != "/data" and not p.startswith(_DATA_PREFIX):
        raise ValueError("output path must start with /data")
    if p == "/data" or p.endswith("/"):
        p = p.rstrip("/") + "/" + default_name
    return p


def build_output_sink(
    spec: Optional[dict],
    *,
    wf_id: str,
    tenant_id: str,
    default_name: str,
    columns: Optional[list[dict]] = None,
) -> Optional[BatchOutputSink]:
    """Resolve an output spec into a sink, or ``None`` when no output is wanted.

    Spec shape (v1): ``{"type": "vfs_data", "path": "/data/results.csv",
    "sheet_name": "Sheet1"}``. ``type`` defaults to ``vfs_data``; ``sheet_name``
    is optional (used only for an Excel ``path``). An unknown type raises.

    ``columns`` is a separate arg (not part of ``spec``) because the user-defined
    output-column schema applies regardless of destination/format. When given, the
    sink emits exactly those columns; otherwise the legacy fixed columns.
    """
    if not spec:
        return None
    kind = (spec.get("type") or "vfs_data") if isinstance(spec, dict) else "vfs_data"
    if kind == "vfs_data":
        raw_path = spec.get("path", "") if isinstance(spec, dict) else ""
        path = normalize_data_output_path(raw_path, default_name=default_name)
        sheet_name = spec.get("sheet_name") if isinstance(spec, dict) else None
        return VfsDataOutputSink(
            wf_id=wf_id, tenant_id=tenant_id, path=path, sheet_name=sheet_name,
            columns=columns,
        )
    raise ValueError(f"unsupported batch output type: {kind!r}")
