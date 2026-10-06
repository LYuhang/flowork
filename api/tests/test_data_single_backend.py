"""Shared tabular decoders consumed by Workflow CLI, without retired file tools."""
import base64
import io

import pytest

from vibecanvas_api.agents.tools.decorator import ToolError
from vibecanvas_api.services.agent_resources import table_io
from vibecanvas_api.services.agent_runtime.cli_tasks import _batch_rows


@pytest.mark.parametrize("text,extension,rows,columns", [
    ("name,age\nAlice,30\nBob,25\n", "csv", [{"name": "Alice", "age": "30"}, {"name": "Bob", "age": "25"}], ["name", "age"]),
    ("name\tage\nAlice\t30\n", "tsv", [{"name": "Alice", "age": "30"}], ["name", "age"]),
    ("name,region\n", "csv", [], ["name", "region"]),
    ('{"a":1}\n{"b":false}\n', "jsonl", [{"a": 1}, {"b": False}], ["a", "b"]),
    ('{"rows":[{"a":0}]}', "json", [{"a": 0}], ["a"]),
    ('[{"a":null}]', "json", [{"a": None}], ["a"]),
])
def test_text_decoders_preserve_rows_headers_and_values(text, extension, rows, columns):
    assert table_io._text_to_rows(text, extension) == (rows, columns)


def _workbook(sheets):
    import openpyxl
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        sheet = workbook.create_sheet(name)
        for row in rows:
            sheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def test_spreadsheet_single_sheet_and_empty_headers():
    data = _workbook({"Rows": [["name", "count"], ["A", 0], ["B", False]]})
    assert table_io._xlsx_to_rows(data, "input.xlsx", "") == ([{"name": "A", "count": 0}, {"name": "B", "count": False}], ["name", "count"])
    assert table_io._xlsx_to_rows(_workbook({"Empty": [["name", "count"]]}), "input.xlsx", "") == ([], ["name", "count"])


@pytest.mark.parametrize("sheet,code", [("", "sheet_required"), ("Missing", "sheet_not_found")])
def test_spreadsheet_ambiguous_or_unknown_sheet_is_explicit(sheet, code):
    data = _workbook({"One": [["value"], [1]], "Two": [["value"], [2]]})
    with pytest.raises(ToolError) as error:
        table_io._xlsx_to_rows(data, "input.xlsx", sheet)
    assert str(error.value) == code
    assert "One" in error.value.message and "Two" in error.value.message


def test_spreadsheet_named_sheet_reads_only_that_sheet():
    data = _workbook({"One": [["value"], [1]], "Two": [["value"], [2]]})
    assert table_io._xlsx_to_rows(data, "input.xlsx", "Two") == ([{"value": 2}], ["value"])


def test_corrupt_spreadsheet_has_a_clean_error():
    with pytest.raises(ToolError) as error:
        table_io._xlsx_to_rows(b"corrupt archive", "input.xlsx", "")
    assert str(error.value) == "read_failed"
    assert error.value.message == "could not read 'input.xlsx' as a spreadsheet"


@pytest.mark.parametrize("extension,data", [("json", b"["), ("jsonl", b"not-json"), ("bin", b"bytes"), ("csv", b"\xff")])
def test_cli_rejects_invalid_tabular_bytes_before_dispatch(extension, data):
    with pytest.raises(ToolError):
        _batch_rows({"format": extension, "data": base64.b64encode(data).decode(), "name": "input." + extension, "sheet": ""})
