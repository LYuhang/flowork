"""Aggregate repeated process measurements without hiding failed runs."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import statistics


def main(root, destination, additional_roots=()):
    roots = [root, *additional_roots]
    summaries = [json.loads((r / 'summary.json').read_text()) for r in roots]
    summary = dict(summaries[0])
    processes = []
    source = {}
    for directory, batch in zip(roots, summaries):
        assert batch['fixture'] == summary['fixture'], 'different fixture settings'
        assert batch['repetitions'] == summary['repetitions'], 'different repetition counts'
        for process in batch['processes']:
            assert process['name'] not in source, 'duplicate measurement name'
            source[process['name']] = directory
            processes.append(process)
    summary['processes'] = processes
    rows = defaultdict(list)
    failures = []
    raw = []
    for process in summary['processes']:
        path = source[process['name']] / (process['name'] + '.json')
        if process['exit_code'] != 0 or not path.exists():
            failures.append(process)
            continue
        run = json.loads(path.read_text())
        if any(not phase['contract_passed'] for phase in run['phases']):
            failures.append(process)
        raw.append(run)
        for phase in run['phases']:
            rows[(run['framework'], run['scenario'], phase['concurrency'])].append((run, phase, process))
    aggregate = []
    for (framework, scenario, concurrency), values in sorted(rows.items()):
        median = lambda getter: round(statistics.median(getter(*v) for v in values), 3)
        aggregate.append({
            'framework': framework, 'scenario': scenario, 'concurrency': concurrency, 'repetitions': len(values),
            'successful_calls': sum(p['success'] for _, p, _ in values),
            'total_calls': sum(p['count'] for _, p, _ in values),
            'median_successful_qps': median(lambda r, p, s: p['successful_qps']),
            'qps_min': min(p['successful_qps'] for _, p, _ in values),
            'qps_max': max(p['successful_qps'] for _, p, _ in values),
            'median_p95_ms': median(lambda r, p, s: p['p95_ms']),
            'median_cpu_ms_per_call': median(lambda r, p, s: p['cpu_ms_per_call']),
            'median_startup_seconds': median(lambda r, p, s: r['startup_seconds']),
            'median_warm_rss_mib': median(lambda r, p, s: r['warm']['Rss_mib']),
            'median_warm_pss_mib': median(lambda r, p, s: r['warm']['Pss_mib']),
            'median_process_peak_rss_mib': median(lambda r, p, s: max(r['lifetime_peak_rss_mib'], s['sampled_peak_rss_mib'])),
            'median_phase_end_rss_mib': median(lambda r, p, s: p['after']['Rss_mib']),
            'median_threads': median(lambda r, p, s: p['after']['threads']),
            'post_cancel_tool_calls': [r['cancellation']['tools_executed_after_cancel'] for r, _, _ in values],
        })
    result = {'fixture': summary['fixture'], 'failures': failures,
              'stopped_reason': [s['stopped_reason'] for s in summaries if s.get('stopped_reason')],
              'expected_repetitions': summary['repetitions'], 'aggregate': aggregate,
              'raw_runs': raw, 'process_measurements': summary['processes'],
              'batch_hosts': [s['host'] for s in summaries]}
    result['missing_repetitions'] = [r for r in aggregate if r['repetitions'] != summary['repetitions']]
    destination.write_text(json.dumps(result, indent=2))
    print('| Framework | Scenario | C | Calls | QPS median [min, max] | P95 ms | Warm RSS MiB | CPU ms/call |')
    print('|---|---|---:|---:|---:|---:|---:|---:|')
    for r in aggregate:
        print(f"| {r['framework']} | {r['scenario']} | {r['concurrency']} | {r['successful_calls']}/{r['total_calls']} | "
              f"{r['median_successful_qps']} [{r['qps_min']}, {r['qps_max']}] | {r['median_p95_ms']} | "
              f"{r['median_warm_rss_mib']} | {r['median_cpu_ms_per_call']} |")
    if failures or result['stopped_reason'] or result['missing_repetitions']:
        raise SystemExit('Incomplete benchmark: inspect JSON failures/stopped_reason')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--additional-root', type=Path, action='append', default=[])
    args = parser.parse_args()
    main(args.root, args.destination, args.additional_root)
