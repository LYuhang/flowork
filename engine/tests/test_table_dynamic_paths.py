"""Regress the full upstream-path template seen during one-shot authoring."""
import pytest
from vibecanvas_engine.nodes import table_io
from vibecanvas_engine.nodes.table_read import TableReadNode
from vibecanvas_engine.nodes.table_write import TableWriteNode
import jsonschema


@pytest.mark.parametrize('cls', [TableReadNode, TableWriteNode])
def test_schema_accepts_upstream_path_reference(cls):
    config = {'file_path': '{{ csv_path }}'}
    if cls is TableWriteNode:
        config['write_mode'] = 'overwrite'
    jsonschema.validate(config, cls.CONFIG_SCHEMA)


@pytest.mark.parametrize('template,inputs,expected', [
    ('{{csv_path}}', {'csv_path': '/run/orders.csv'}, '/run/orders.csv'),
    ('{{ csv_path }}', {'csv_path': '/mount/input.csv'}, '/mount/input.csv'),
    ('/run/{{name}}.csv', {'name': 'orders'}, '/run/orders.csv'),
])
def test_resolve_upstream_and_filename_templates(template, inputs, expected):
    assert table_io.resolve_file_path(template, inputs) == expected


@pytest.mark.parametrize('value', ['/etc/passwd', '/data/orders.csv', 'orders.csv',
    '/run/../etc/passwd', '/run/../../run/orders.csv', '/run/', '/mount/..',
    '/run/{{missing}}.csv', '/run/a\x00.csv', 123, {}, '', None])
def test_reject_invalid_dynamic_path_before_io(value):
    with pytest.raises(ValueError):
        table_io.resolve_file_path('{{path}}', {'path': value})


def test_missing_path_field_fails():
    with pytest.raises(ValueError, match='non-empty string'):
        table_io.resolve_file_path('/run/{{missing}}.csv', {})


def test_reject_symlink_escape(monkeypatch):
    realpath = table_io.os.path.realpath
    monkeypatch.setattr(table_io.os.path, 'realpath',
        lambda p: '/etc/passwd' if p == '/run/link.csv' else realpath(p))
    with pytest.raises(ValueError, match='remain under'):
        table_io.resolve_file_path('{{path}}', {'path': '/run/link.csv'})


@pytest.mark.parametrize('cls', [TableReadNode, TableWriteNode])
def test_nodes_apply_path_guard_before_file_io(cls, monkeypatch):
    node = cls.__new__(cls)
    node.node_config = {'file_path': '{{path}}', 'file_format': 'csv', 'write_mode': 'overwrite'}
    def unexpected(*args, **kwargs):
        pytest.fail('File I/O happened before validating the resolved path')
    monkeypatch.setattr(table_io, 'read_rows', unexpected)
    monkeypatch.setattr(table_io, 'write_rows', unexpected)
    result = node({'path': '/etc/passwd'}, {})
    assert result['status'] == 'error'
    assert 'remain under' in result['error_message']


def test_read_consumes_exact_upstream_path(monkeypatch):
    node = TableReadNode.__new__(TableReadNode)
    node.node_config = {'file_path': '{{csv_path}}'}
    observed = []
    def read(path, fmt, sheet):
        observed.append((path, fmt))
        return [{'order_id': 'A'}]
    monkeypatch.setattr(table_io, 'read_rows', read)
    result = node({'csv_path': '/run/orders.csv'}, {})
    assert result['status'] == 'success'
    assert result['output']['row_count'] == 1
    assert observed == [('/run/orders.csv', 'csv')]
