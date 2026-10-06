"""Unit tests for the batch output-destination abstraction (no I/O).

Covers path normalization (the /data allowlist) and the sink factory's
dispatch. The actual VFS write (VfsDataOutputSink.write) is exercised through
the batch_exec integration path, not here — these stay pure so they run fast and
without a DB.
"""
import pytest

import io
import json

from vibecanvas_api.services.batch_output import (
    BatchOutputSink,
    VfsDataOutputSink,
    build_output_sink,
    normalize_data_output_path,
    project_rows,
    sanitize_sheet_name,
    serialize_results,
)

_OUT_ROWS = [
    {"index": 0, "status": "success", "attempt": 1, "input": {"x": "1"}, "output": {"y": "2"}, "error": None, "execution_time": 0.5},
    {"index": 1, "status": "error", "attempt": 2, "input": {"x": "3"}, "output": None, "error": {"message": "boom"}, "execution_time": None},
]

_COLS = ["index", "status", "attempt", "input", "output", "error", "execution_time"]


def test_posix_output_is_visible_in_workspace_without_database(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from vibecanvas_api.services import batch_output
    from vibecanvas_api.services.workspace_storage import PosixWorkspaceStorage, WorkspaceIdentity

    monkeypatch.setattr(batch_output.app_config, 'workspace_storage_backend', 'posix')
    monkeypatch.setattr(batch_output.app_config, 'workspace_storage_root', str(tmp_path))
    monkeypatch.setattr(batch_output, 'run_in_short_session', Mock(side_effect=AssertionError('no DB write')))
    monkeypatch.setattr(batch_output, 'get_object_store', Mock(side_effect=AssertionError('no object copy')))
    sink = VfsDataOutputSink(wf_id='workflow', tenant_id='tenant', path='/data/results.jsonl')
    assert sink.write_rows(_OUT_ROWS) == '/data/results.jsonl'
    storage = PosixWorkspaceStorage(str(tmp_path))
    identity = WorkspaceIdentity('tenant', 'workflow', 'workflow')
    result = b''.join(storage.iter_bytes(identity, 'data/results.jsonl'))
    assert result == serialize_results(_OUT_ROWS, path=sink.path)[0]
    sink.write_rows(_OUT_ROWS[:1])
    assert len(b''.join(storage.iter_bytes(identity, 'data/results.jsonl')).splitlines()) == 1


class TestNormalizeDataOutputPath:
    def test_keeps_a_valid_data_path(self):
        assert normalize_data_output_path("/data/out.csv", default_name="r.csv") == "/data/out.csv"

    def test_adds_leading_slash(self):
        assert normalize_data_output_path("data/out.csv", default_name="r.csv") == "/data/out.csv"

    def test_bare_data_dir_gets_default_name(self):
        assert normalize_data_output_path("/data", default_name="r.csv") == "/data/r.csv"

    def test_trailing_slash_gets_default_name(self):
        assert normalize_data_output_path("/data/sub/", default_name="r.csv") == "/data/sub/r.csv"

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_empty_raises(self, bad):
        with pytest.raises(ValueError):
            normalize_data_output_path(bad, default_name="r.csv")

    @pytest.mark.parametrize("bad", ["/mount/out.csv", "/etc/passwd", "/memory/x"])
    def test_outside_data_raises(self, bad):
        with pytest.raises(ValueError):
            normalize_data_output_path(bad, default_name="r.csv")

    def test_traversal_raises(self):
        with pytest.raises(ValueError):
            normalize_data_output_path("/data/../etc/x", default_name="r.csv")


class TestBuildOutputSink:
    def test_none_spec_returns_none(self):
        assert build_output_sink(None, wf_id="w", tenant_id="t", default_name="r.csv") is None

    def test_vfs_data_builds_sink_with_normalized_path(self):
        sink = build_output_sink(
            {"type": "vfs_data", "path": "/data/out.csv"},
            wf_id="w", tenant_id="t", default_name="r.csv",
        )
        assert isinstance(sink, VfsDataOutputSink)
        assert isinstance(sink, BatchOutputSink)
        assert sink.path == "/data/out.csv"

    def test_type_defaults_to_vfs_data(self):
        sink = build_output_sink(
            {"path": "/data/out.csv"}, wf_id="w", tenant_id="t", default_name="r.csv",
        )
        assert isinstance(sink, VfsDataOutputSink)

    def test_bad_path_raises(self):
        with pytest.raises(ValueError):
            build_output_sink(
                {"type": "vfs_data", "path": "/mount/x.csv"},
                wf_id="w", tenant_id="t", default_name="r.csv",
            )

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError):
            build_output_sink(
                {"type": "unsupported_remote_sheet", "path": "x"},
                wf_id="w", tenant_id="t", default_name="r.csv",
            )

    def test_sheet_name_flows_to_sink(self):
        sink = build_output_sink(
            {"type": "vfs_data", "path": "/data/out.xlsx", "sheet_name": "Results"},
            wf_id="w", tenant_id="t", default_name="r.csv",
        )
        assert isinstance(sink, VfsDataOutputSink)
        assert sink.sheet_name == "Results"


class TestSerializeResults:
    def test_csv_by_extension(self):
        import csv as _csv

        data, ct = serialize_results(_OUT_ROWS, path="/data/r.csv")
        assert ct == "table/csv"
        # csv.writer quotes the JSON cells (they contain "), so round-trip to
        # assert the unescaped values rather than a raw substring.
        rows = list(_csv.reader(io.StringIO(data.decode())))
        assert rows[0] == _COLS
        assert rows[1][3] == '{"x": "1"}'
        assert rows[1][6] == "0.5000"  # execution_time formatted
        assert rows[2][5] == "boom"
        assert rows[2][6] == ""  # None execution_time → blank

    def test_tsv_by_extension(self):
        data, ct = serialize_results(_OUT_ROWS, path="/data/r.tsv")
        assert ct == "table/tsv"
        assert "\t" in data.decode().splitlines()[0]

    def test_jsonl_by_extension(self):
        data, ct = serialize_results(_OUT_ROWS, path="/data/r.jsonl")
        assert ct == "table/jsonl"
        first = json.loads(data.decode().splitlines()[0])
        assert set(first) == set(_COLS)

    def test_unknown_extension_defaults_to_csv(self):
        _, ct = serialize_results(_OUT_ROWS, path="/data/r.dat")
        assert ct == "table/csv"

    def test_xlsx_by_extension_roundtrips_via_pandas(self):
        import pandas as pd

        data, ct = serialize_results(_OUT_ROWS, path="/data/r.xlsx", sheet_name="My Results")
        assert ct.endswith("spreadsheetml.sheet")
        assert data[:2] == b"PK"  # xlsx is a zip
        xls = pd.ExcelFile(io.BytesIO(data), engine="openpyxl")
        assert xls.sheet_names == ["My Results"]
        df = xls.parse("My Results")
        assert list(df.columns) == _COLS
        assert len(df) == 2


# Rows whose `output` is the engine previous_outputs map: {node: {field: val}}.
_NODE_ROWS = [
    {
        "i": 0,
        "input": {"x": "1"},
        "output": {"End": {"verdict": "reject", "score": {"p": 0.9}}},
        "error": "",
        "execution_time": 0.5,
        "ok": True,
    },
    {
        "i": 1,
        "input": {"x": "3"},
        # Took a different branch: no "End" node, has "Approve" instead.
        "output": {"Approve": {"verdict": "approve"}},
        "error": "oops",
        "execution_time": None,
        "ok": False,
    },
    {
        "i": 2,
        "input": {"x": "5"},
        # output[node] is not a dict → field columns fall back.
        "output": {"End": "not-a-dict"},
        "error": "",
        "execution_time": 1.25,
        "ok": True,
    },
]

_SCHEMA = [
    {"kind": "index", "name": "row"},
    {"kind": "status", "name": "status"},
    {"kind": "error", "name": "err"},
    {"kind": "execution_time", "name": "secs"},
    {"kind": "field", "name": "Verdict", "node": "End", "field": "verdict"},
    {"kind": "field", "name": "Score", "node": "End", "field": "score"},
]


class TestProjectRows:
    def test_headers_are_column_names_in_order(self):
        headers, _ = project_rows(_NODE_ROWS, _SCHEMA)
        assert headers == ["row", "status", "err", "secs", "Verdict", "Score"]

    def test_resolves_index_exec_error_and_fields(self):
        _, rows = project_rows(_NODE_ROWS, _SCHEMA)
        # row 0: index=0, status derived from ok, error "", exec 0.5→"0.5000",
        # End.verdict, End.score (dict→JSON)
        assert rows[0] == ["0", "success", "", "0.5000", "reject", '{"p": 0.9}']

    def test_execution_time_none_is_blank(self):
        _, rows = project_rows(_NODE_ROWS, _SCHEMA)
        assert rows[1][3] == ""  # execution_time None
        assert rows[1][2] == "oops"  # error string

    def test_missing_node_or_field_falls_back_to_empty(self):
        _, rows = project_rows(_NODE_ROWS, _SCHEMA)
        # row 1 has no "End" node at all → empty cells, no raise.
        assert rows[1][4] == ""
        assert rows[1][5] == ""

    def test_default_used_when_field_absent(self):
        schema = [
            {"kind": "field", "name": "V", "node": "End", "field": "verdict",
             "default": "N/A"},
        ]
        _, rows = project_rows(_NODE_ROWS, schema)
        assert rows[0] == ["reject"]   # present → real value
        assert rows[1] == ["N/A"]      # node absent → default
        assert rows[2] == ["N/A"]      # output[node] not a dict → default

    def test_empty_default_is_treated_as_no_default(self):
        schema = [
            {"kind": "field", "name": "V", "node": "End", "field": "verdict",
             "default": ""},
        ]
        _, rows = project_rows(_NODE_ROWS, schema)
        assert rows[1] == [""]

    def test_node_output_not_a_dict_falls_back(self):
        # row 2: output["End"] == "not-a-dict" → field cells empty.
        _, rows = project_rows(_NODE_ROWS, _SCHEMA)
        assert rows[2][4] == ""
        assert rows[2][5] == ""

    def test_malformed_column_degrades_to_empty(self):
        schema = [{"kind": "field", "name": "V", "node": "End", "field": "verdict"},
                  "not-a-dict", {"kind": "bogus", "name": "B"}]
        headers, rows = project_rows(_NODE_ROWS, schema)
        assert headers == ["V", "", "B"]
        assert rows[0] == ["reject", "", ""]


class TestSerializeResultsWithColumns:
    def test_csv_header_and_cells_from_schema(self):
        import csv as _csv

        data, ct = serialize_results(_NODE_ROWS, path="/data/r.csv", columns=_SCHEMA)
        assert ct == "table/csv"
        rows = list(_csv.reader(io.StringIO(data.decode())))
        assert rows[0] == ["row", "status", "err", "secs", "Verdict", "Score"]
        assert rows[1] == ["0", "success", "", "0.5000", "reject", '{"p": 0.9}']
        # branch row: no End node → blank verdict/score
        assert rows[2][4] == ""

    def test_columns_none_yields_legacy_columns(self):
        import csv as _csv

        data, _ = serialize_results(_NODE_ROWS, path="/data/r.csv", columns=None)
        rows = list(_csv.reader(io.StringIO(data.decode())))
        assert rows[0] == _COLS

    def test_empty_columns_list_yields_legacy_columns(self):
        import csv as _csv

        data, _ = serialize_results(_NODE_ROWS, path="/data/r.csv", columns=[])
        rows = list(_csv.reader(io.StringIO(data.decode())))
        assert rows[0] == _COLS

    def test_xlsx_with_columns_roundtrips_via_pandas(self):
        import pandas as pd

        data, ct = serialize_results(
            _NODE_ROWS, path="/data/r.xlsx", sheet_name="Out", columns=_SCHEMA,
        )
        assert ct.endswith("spreadsheetml.sheet")
        xls = pd.ExcelFile(io.BytesIO(data), engine="openpyxl")
        df = xls.parse("Out")
        assert list(df.columns) == ["row", "status", "err", "secs", "Verdict", "Score"]
        assert len(df) == 3


class TestBuildOutputSinkColumns:
    def test_columns_flow_to_sink(self):
        sink = build_output_sink(
            {"type": "vfs_data", "path": "/data/out.csv"},
            wf_id="w", tenant_id="t", default_name="r.csv", columns=_SCHEMA,
        )
        assert isinstance(sink, VfsDataOutputSink)
        assert sink.columns == _SCHEMA

    def test_columns_default_none(self):
        sink = build_output_sink(
            {"type": "vfs_data", "path": "/data/out.csv"},
            wf_id="w", tenant_id="t", default_name="r.csv",
        )
        assert sink.columns is None


class TestSanitizeSheetName:
    def test_default_when_blank(self):
        assert sanitize_sheet_name(None) == "Sheet1"
        assert sanitize_sheet_name("  ") == "Sheet1"

    def test_strips_forbidden_chars(self):
        assert sanitize_sheet_name("a/b:c*?[d]") == "a_b_c___d_"

    def test_truncates_to_31(self):
        assert len(sanitize_sheet_name("x" * 40)) == 31
