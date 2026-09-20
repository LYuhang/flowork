#!/usr/bin/env python3
"""Apply Flowork's reviewable policy to a complete Grype JSON report.

Grype still records every finding. The release gate blocks fixed High/Critical
findings unless a narrow, dated exception applies. Unfixed findings remain in
the uploaded report so an upstream release can be adopted when one exists.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

DEFAULT_POLICY = Path(__file__).with_name("container-vulnerability-policy.json")
_VERSION_PREFIX = re.compile(r"^\*?(\d+)\.(\d+)(?:\.|$)")


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return value


def _expiry(value: object, *, context: str, today: dt.date) -> dt.date:
    if not isinstance(value, str):
        raise TypeError(f"{context}.expires must be an ISO date")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{context}.expires must be an ISO date") from exc
    if parsed < today:
        raise ValueError(f"{context} expired on {parsed.isoformat()}")
    return parsed


def _validate_policy(policy: dict[str, Any], *, today: dt.date) -> None:
    if policy.get("schema_version") != 1:
        raise ValueError("policy.schema_version must be 1")
    if policy.get("blocking_severities") != ["High", "Critical"]:
        raise ValueError("policy must block High and Critical severities")

    if "advisory_labels" in policy:
        raise ValueError(
            "policy.advisory_labels is unsupported; use exact, dated exceptions"
        )

    exceptions = policy.get("exceptions")
    if not isinstance(exceptions, list):
        raise TypeError("policy.exceptions must be an array")
    ids: set[str] = set()
    for index, rule in enumerate(exceptions):
        context = f"exceptions[{index}]"
        if not isinstance(rule, dict):
            raise TypeError(f"{context} must be an object")
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not rule_id or rule_id in ids:
            raise ValueError(f"{context}.id must be unique and non-empty")
        ids.add(rule_id)
        _expiry(rule.get("expires"), context=context, today=today)
        if not isinstance(rule.get("reason"), str) or not rule["reason"].strip():
            raise ValueError(f"{context}.reason is required")
        labels = rule.get("labels")
        if (
            not isinstance(labels, list)
            or not labels
            or not all(isinstance(item, str) and item for item in labels)
        ):
            raise ValueError(f"{context}.labels must be a non-empty string array")
        condition = rule.get("condition")
        if condition not in {"all", "locations-only", "python-no-same-minor-fix"}:
            raise ValueError(f"{context}.condition is unsupported")
        vulnerabilities = rule.get("vulnerabilities")
        if (
            not isinstance(vulnerabilities, list)
            or not vulnerabilities
            or not all(
                isinstance(item, str) and item and item != "*"
                for item in vulnerabilities
            )
        ):
            raise ValueError(f"{context}.vulnerabilities must contain exact IDs")
        package = rule.get("package")
        if package is not None and (
            not isinstance(package, dict)
            or not isinstance(package.get("name"), str)
            or not isinstance(package.get("type"), str)
        ):
            raise ValueError(f"{context}.package must contain name and type")
        if condition == "locations-only" and (
            not isinstance(rule.get("locations"), list) or not rule["locations"]
        ):
            raise ValueError(f"{context}.locations is required")


def _same_python_minor_fix(match: dict[str, Any]) -> bool:
    artifact = match.get("artifact") or {}
    installed = _VERSION_PREFIX.match(str(artifact.get("version", "")))
    if installed is None:
        return True
    versions = ((match.get("vulnerability") or {}).get("fix") or {}).get(
        "versions"
    ) or []
    return any(
        candidate is not None and candidate.groups() == installed.groups()
        for candidate in (_VERSION_PREFIX.match(str(version)) for version in versions)
    )


def _matches_rule(label: str, match: dict[str, Any], rule: dict[str, Any]) -> bool:
    if label not in rule["labels"]:
        return False
    vulnerability = match.get("vulnerability") or {}
    artifact = match.get("artifact") or {}
    accepted_ids = rule.get("vulnerabilities")
    if accepted_ids is not None and vulnerability.get("id") not in accepted_ids:
        return False
    package = rule.get("package")
    if package is not None and (
        artifact.get("name") != package["name"]
        or artifact.get("type") != package["type"]
    ):
        return False

    condition = rule["condition"]
    if condition == "python-no-same-minor-fix":
        return not _same_python_minor_fix(match)
    if condition == "locations-only":
        allowed = set(rule["locations"])
        actual = {
            location.get("path")
            for location in artifact.get("locations") or []
            if isinstance(location, dict)
        }
        return bool(actual) and actual <= allowed
    return True


def evaluate(
    *, label: str, report: dict[str, Any], policy: dict[str, Any], today: dt.date
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], str]], int]:
    _validate_policy(policy, today=today)
    severities = set(policy["blocking_severities"])
    fixed: list[dict[str, Any]] = []
    observed_unfixed = 0
    for match in report.get("matches") or []:
        vulnerability = match.get("vulnerability") or {}
        if vulnerability.get("severity") not in severities:
            continue
        if (vulnerability.get("fix") or {}).get("state") != "fixed":
            observed_unfixed += 1
            continue
        fixed.append(match)

    blocking: list[dict[str, Any]] = []
    accepted: list[tuple[dict[str, Any], str]] = []
    for match in fixed:
        rule = next(
            (
                rule
                for rule in policy["exceptions"]
                if _matches_rule(label, match, rule)
            ),
            None,
        )
        if rule is None:
            blocking.append(match)
        else:
            accepted.append((match, rule["id"]))
    return blocking, accepted, observed_unfixed


def _finding(match: dict[str, Any]) -> str:
    vulnerability = match.get("vulnerability") or {}
    artifact = match.get("artifact") or {}
    locations = ",".join(
        str(item.get("path"))
        for item in artifact.get("locations") or []
        if isinstance(item, dict) and item.get("path")
    )
    return (
        f"{vulnerability.get('severity')} {vulnerability.get('id')} "
        f"{artifact.get('name')}@{artifact.get('version')} locations={locations or '-'}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    args = parser.parse_args(argv)

    today = dt.datetime.now(dt.timezone.utc).date()
    try:
        blocking, accepted, observed_unfixed = evaluate(
            label=args.label,
            report=_load_object(args.report),
            policy=_load_object(args.policy),
            today=today,
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(
            f"container_vulnerability_policy=error label={args.label} error={exc}",
            file=sys.stderr,
        )
        return 2

    for rule_id, count in sorted(Counter(rule_id for _, rule_id in accepted).items()):
        print(f"accepted[{rule_id}] count={count}", file=sys.stderr)
    for match in blocking:
        print(f"blocking {_finding(match)}", file=sys.stderr)
    status = "fail" if blocking else "pass"
    print(
        f"container_vulnerability_policy={status} label={args.label} "
        f"blocking={len(blocking)} accepted={len(accepted)} "
        f"unfixed_observed={observed_unfixed}"
    )
    return 2 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
