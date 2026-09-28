import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('verify', Path(__file__).parents[1] / 'verify_order_audit_api.py')
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class ExternalVerificationTests(unittest.TestCase):
    def result(self):
        expected = verify.cases()[0][2]
        result = {'batch_id': 'test', **expected, 'service_health': 'ok', 'summary': '摘要', 'report': '# Report'}
        return {'outputs': {'__end__': result}}, expected

    def test_valid_output(self):
        body, expected = self.result()
        self.assertEqual(verify.validate_response(body, expected, 'test')['total'], 290)

    def test_wrong_totals_types_and_missing_fields_fail(self):
        for field, value in [('total', 291), ('total', float('nan')), ('audit_pass', 'true'),
                             ('accepted_count', 2.0), ('summary', ''), ('service_health', 'failed')]:
            with self.subTest(field=field, value=value):
                body, expected = self.result()
                body['outputs']['__end__'][field] = value
                with self.assertRaises(AssertionError):
                    verify.validate_response(body, expected, 'test')

    def test_wrapper_without_executed_end_is_not_success(self):
        with self.assertRaises(AssertionError):
            verify.validate_response({'status': 'succeeded'}, {}, 'test')

    def test_disconnected_nodes_do_not_count_as_coverage(self):
        types = ['StartNode'] + sorted(verify.NODE_TYPES - {'StartNode'})
        graph = {f'node_{i}': {'node_type': kind, 'node_name': kind,
                             'children': [f'node_{i+1}'] if i+1 < len(types) else []}
                 for i, kind in enumerate(types)}
        self.assertEqual(len(verify.validate_graph(graph)), 15)
        graph['node_0']['children'] = []
        with self.assertRaisesRegex(AssertionError, 'disconnected'):
            verify.validate_graph(graph)

    def test_loop_body_outputs_count(self):
        self.assertEqual(verify.executed_node_names({'loop': {'loop_output': [{'price_line': {'total': 240}}]}}),
                         {'loop', 'price_line'})


if __name__ == '__main__':
    unittest.main()
