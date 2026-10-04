"""Bounded local framework comparison, sequential fresh worker processes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]


def read_memory(pid):
    data = {}
    try:
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                if k in {"Rss", "Pss", "Private_Clean", "Private_Dirty", "SwapPss"}:
                    data[k + "_mib"] = round(int(v.split()[0])/1024, 3)
    except (OSError, ValueError):
        pass
    return data


def main(args):
    target = Path(args.output).resolve()
    target.mkdir(parents=True, exist_ok=False)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
           "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": str(REPO/"api/src") + ":" + str(REPO/"engine/src"),
           "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
           "LANGCHAIN_TRACING_V2": "false", "OTEL_SDK_DISABLED": "true",
           "TOKENIZERS_PARALLELISM": "false", "OPENAI_API_KEY": "benchmark-local-only"}
    summary = {"fixture": {"model_delay_seconds": args.model_delay, "tool_delay_seconds": .01,
                           "model_rounds_per_call": 3, "business_tools_per_call": 2,
                           "note": "local fake model; not external-model quality or production QPS"},
               "repetitions": args.repetitions, "processes": [],
               "frameworks": args.frameworks.split(','), "scenarios": args.scenarios.split(','),
               "phases": args.phases,
               "host": {"cpu_count": os.cpu_count(), "meminfo": Path('/proc/meminfo').read_text(),
                        "loadavg_start": Path('/proc/loadavg').read_text()},
               "python": {"baseline": args.baseline_python, "candidates": args.candidate_python}}
    server_log = (target / "server.stderr.log").open("w")
    server = subprocess.Popen([args.baseline_python, str(HERE/"mock_server.py"), "--model-delay", str(args.model_delay)],
                              env=env, stdout=subprocess.PIPE, stderr=server_log, text=True)
    try:
        port = json.loads(server.stdout.readline())["port"]
        url = f"http://127.0.0.1:{port}"
        cases = [(r, f, s) for r in range(args.repetitions) for f in args.frameworks.split(',')
                 for s in args.scenarios.split(',')]
        random.Random(20261004).shuffle(cases)
        for repetition, framework, scenario in cases:
            available = next(int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()
                             if line.startswith('MemAvailable:')) / 1024
            if available < args.min_available_mib or shutil.disk_usage(target).free < 64*1024*1024:
                summary['stopped_reason'] = 'host_resource_guard'
                break
            name = f"{framework}-{scenario}-{repetition}"
            python = args.baseline_python if framework in {"langchain", "flowork"} else args.candidate_python
            command = [python, str(HERE/'worker.py'), '--framework', framework, '--scenario', scenario,
                       '--url', url, '--result', str(target/(name+'.json')), '--phases', args.phases,
                       '--warmup', str(args.warmup)]
            samples = []
            start = time.monotonic()
            killed = None
            with (target/(name+'.log')).open('w') as log:
                child = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                while child.poll() is None:
                    sample = read_memory(child.pid)
                    samples.append({'elapsed': round(time.monotonic()-start, 3), **sample})
                    if sample.get('Rss_mib', 0) > args.max_worker_mib or time.monotonic()-start > 180:
                        killed = 'worker_resource_guard'
                        child.kill()
                        break
                    time.sleep(.025)
                child.wait(timeout=10)
            (target/(name+'.memory.json')).write_text(json.dumps(samples))
            record = {'name': name, 'framework': framework, 'scenario': scenario, 'repetition': repetition,
                      'exit_code': child.returncode, 'wall_seconds': round(time.monotonic()-start, 3),
                      'sampled_peak_rss_mib': max((x.get('Rss_mib', 0) for x in samples), default=0),
                      'sampled_peak_pss_mib': max((x.get('Pss_mib', 0) for x in samples), default=0)}
            if killed:
                record['stopped_reason'] = killed
            summary['processes'].append(record)
            (target/'summary.json').write_text(json.dumps(summary, indent=2))
            print(json.dumps(record), flush=True)
        summary['host']['loadavg_end'] = Path('/proc/loadavg').read_text()
        (target/'summary.json').write_text(json.dumps(summary, indent=2))
    finally:
        server.terminate()
        server.wait(timeout=10)
        server_log.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--baseline-python', required=True)
    p.add_argument('--candidate-python', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--frameworks', default='langchain,flowork,pydantic,agents,smolagents,smolagents16')
    p.add_argument('--scenarios', default='local,broker_http')
    p.add_argument('--repetitions', type=int, default=3)
    p.add_argument('--phases', default='1:16,4:32,16:64')
    p.add_argument('--warmup', type=int, default=3)
    p.add_argument('--model-delay', type=float, default=.1)
    p.add_argument('--min-available-mib', type=int, default=250)
    p.add_argument('--max-worker-mib', type=int, default=350)
    main(p.parse_args())
