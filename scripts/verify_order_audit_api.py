#!/usr/bin/env python3
"""Call the automation example as an external client, without a Flowork login.

Only invokes the existing endpoint; never creates or repairs a Workflow.
Read the one-time deployment JSON via --credential-file or an API key via
FLOWORK_DEPLOYMENT_API_KEY. Credentials never enter command arguments/reports.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid


NODE_TYPES = frozenset({
    "StartNode", "EndNode", "CodeNode", "TransformNode", "TemplateNode",
    "ConditionNode", "LoopBeginNode", "LoopEndNode", "ParallelStartNode",
    "ParallelEndNode", "HTTPRequestNode", "TableWriteNode", "TableReadNode",
    "PromptNode", "SubAgentNode",
})


def cases():
    def order(name, quantity, price):
        return {"order_id": name, "quantity": quantity, "unit_price": price}

    return [
        ("standard", [order("A", 2, 120), order("B", 1, 50)],
         {"status": "ok", "accepted_count": 2, "rejected_count": 0, "duplicate_count": 0,
          "subtotal": 290, "discount_rate": 0, "total": 290, "risk_route": "standard", "audit_pass": True}),
        ("review_mixed", [order("A", 2, 120), order("B", 3, 200), order("C", 1, 160),
                          order("A", 9, 999), order("D", -1, 30)],
         {"status": "ok", "accepted_count": 3, "rejected_count": 1, "duplicate_count": 1,
          "subtotal": 1000, "discount_rate": 0.1, "total": 900, "risk_route": "review", "audit_pass": True}),
        ("empty", [],
         {"status": "invalid", "accepted_count": 0, "rejected_count": 0, "duplicate_count": 0,
          "subtotal": 0, "discount_rate": 0, "total": 0, "risk_route": "invalid", "audit_pass": False}),
    ]


def validate_response(body: dict, expected: dict, batch_id: str) -> dict:
    outputs = body.get("outputs")
    if not isinstance(outputs, dict) or not isinstance(outputs.get("__end__"), dict):
        raise AssertionError("API did not return the executed EndNode outputs")
    result = outputs["__end__"]
    for field, value in {"batch_id": batch_id, **expected}.items():
        actual = result.get(field)
        if field.endswith("_count"):
            valid = type(actual) is int and actual == value
        elif type(value) in (int, float):
            valid = type(actual) in (int, float) and math.isfinite(actual) and math.isclose(
                actual, value, rel_tol=0, abs_tol=0.000001,
            )
        else:
            valid = type(actual) is type(value) and actual == value
        if not valid:
            raise AssertionError(f"Incorrect {field}: expected {value!r}, received {actual!r}")
    if result["status"] == "ok":
        if result.get("service_health") != "ok":
            raise AssertionError("HTTP health check did not succeed")
        for field in ("summary", "report"):
            if not isinstance(result.get(field), str) or not result[field].strip():
                raise AssertionError(f"Missing generated {field}")
    elif not any(isinstance(result.get(key), str) and result[key].strip()
                 for key in ("reason", "summary", "report")):
        raise AssertionError("Invalid batch has no readable reason")
    return result


def validate_graph(raw: dict) -> dict:
    graph = raw.get("workflow", raw)
    nodes = {key: node for key, node in graph.items()
             if isinstance(node, dict) and "node_type" in node}
    missing = NODE_TYPES - {node["node_type"] for node in nodes.values()}
    if missing:
        raise AssertionError("Saved graph lacks node types: " + ", ".join(sorted(missing)))
    starts = [key for key, node in nodes.items() if node["node_type"] == "StartNode"]
    if len(starts) != 1:
        raise AssertionError("Saved graph must contain one StartNode")
    reached, pending = set(), starts[:]
    while pending:
        key = pending.pop()
        if key in reached:
            continue
        if key not in nodes:
            raise AssertionError("Saved graph contains a dangling edge")
        reached.add(key)
        pending.extend(nodes[key].get("children", []))
    if reached != set(nodes):
        raise AssertionError("Saved graph contains disconnected decorative nodes")
    return nodes


def executed_node_names(outputs: dict) -> set[str]:
    names = set(outputs)
    for value in outputs.values():
        if isinstance(value, dict):
            for iteration in value.get("loop_output", []) or []:
                if isinstance(iteration, dict):
                    names.update(executed_node_names(iteration))
    return names


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a deployment credential to another URL.


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--credential-file", type=Path)
    parser.add_argument("--workflow-json", type=Path, help="Optional independently downloaded saved graph")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    origin = urlsplit(args.endpoint)
    if origin.scheme not in {"http", "https"} or origin.username or origin.password or origin.query or origin.fragment:
        parser.error("Use the exact HTTP(S) invocation URL, without credentials/query/fragment")
    key = os.environ.get("FLOWORK_DEPLOYMENT_API_KEY", "")
    if args.credential_file:
        key = json.loads(args.credential_file.read_text())["api_key"]
    if not key:
        parser.error("Provide --credential-file or FLOWORK_DEPLOYMENT_API_KEY")

    report = {"endpoint": args.endpoint, "client": "external_bearer_only", "cases": [], "passed": False}
    try:
        nodes = {}
        observed = set()
        if args.workflow_json:
            nodes = validate_graph(json.loads(args.workflow_json.read_text()))
            report["node_types"] = sorted({node["node_type"] for node in nodes.values()})
            report["node_count"] = len(nodes)
        opener = build_opener(NoRedirect())
        # Missing credentials must not execute or disclose the workflow.
        try:
            opener.open(Request(args.endpoint, data=b"{}", headers={"Content-Type": "application/json"}), timeout=20)
            raise AssertionError("Endpoint accepted an unauthenticated invocation")
        except HTTPError as exc:
            if exc.code != 401:
                raise AssertionError(f"Expected unauthenticated HTTP 401, received {exc.code}") from exc
        report["unauthenticated_http"] = 401
        for name, orders, expected in cases():
            batch_id = "verify-" + name + "-" + uuid.uuid4().hex[:8]
            request = Request(args.endpoint, data=json.dumps({"batch_id": batch_id, "orders": orders}).encode(),
                              headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
            started = time.monotonic()
            with opener.open(request, timeout=args.timeout) as response:
                body = json.load(response)
            result = validate_response(body, expected, batch_id)
            observed.update(executed_node_names(body["outputs"]))
            if name == "empty" and any(node["node_name"] in body["outputs"] for node in nodes.values()
                                       if node["node_type"] in {"PromptNode", "SubAgentNode"}):
                raise AssertionError("Empty batch unexpectedly called a model node")
            report["cases"].append({"case": name, "passed": True, "elapsed_s": round(time.monotonic() - started, 2),
                                    "result": result, "output_nodes": sorted(body["outputs"])})
            print(json.dumps({"case": name, "passed": True, "total": result["total"],
                              "risk_route": result["risk_route"]}), flush=True)
            time.sleep(1.1)  # Respect the example's one-request-per-second cap.
        # LoopEndNode has no separate output: its paired LoopBeginNode emits the
        # completed loop_output. Every other node must have runtime evidence.
        missing_outputs = {node["node_name"] for node in nodes.values()
                           if node["node_type"] != "LoopEndNode"} - observed
        if missing_outputs:
            raise AssertionError("Nodes lack runtime outputs: " + ", ".join(sorted(missing_outputs)))
        report["executed_output_nodes"] = sorted(observed)
        report["passed"] = True
        return 0
    except HTTPError as exc:
        report["error"] = f"Invocation HTTP {exc.code}"
        raise RuntimeError(report["error"]) from None
    except Exception as exc:
        report["error"] = str(exc)
        raise
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
