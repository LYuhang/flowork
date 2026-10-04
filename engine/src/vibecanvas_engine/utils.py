# -*- coding: utf-8 -*-

import re
import time
import functools
import traceback
import ast
import inspect
import json
from typing import Any, Mapping


_VALIDATION_ERROR_TYPES = None


class InputNormalizeError(ValueError):
    """Raised when a submitted execution input cannot match its declared type."""


def normalize_field_type(field_type: Any) -> str:
    t = str(field_type or "string").strip().lower()
    if t in {"str", "text"}:
        return "string"
    if t in {"int"}:
        return "integer"
    if t in {"float", "double"}:
        return "number"
    if t in {"bool"}:
        return "boolean"
    if t in {"list", "tuple"}:
        return "array"
    if t in {"dict", "map", "json"}:
        return "object"
    return t


def _parse_input_literal(raw: str) -> Any:
    s = raw.strip()
    if s == "":
        raise InputNormalizeError("empty literal")
    try:
        return json.loads(s)
    except Exception:
        pass
    try:
        return ast.literal_eval(s)
    except Exception as exc:
        raise InputNormalizeError(str(exc)) from exc


def normalize_value_for_type(value: Any, field_type: Any) -> Any:
    t = normalize_field_type(field_type)
    if t == "string":
        if isinstance(value, str):
            return value
        if value is None:
            return ""
        return str(value)
    if t == "integer":
        if isinstance(value, bool):
            raise InputNormalizeError("boolean is not a valid integer")
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            s = value.strip()
            if s == "":
                raise InputNormalizeError("empty value is not a valid integer")
            try:
                return int(s)
            except ValueError as exc:
                raise InputNormalizeError(f"{value!r} is not a valid integer") from exc
        raise InputNormalizeError(f"{type(value).__name__} is not a valid integer")
    if t == "number":
        if isinstance(value, bool):
            raise InputNormalizeError("boolean is not a valid number")
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            s = value.strip()
            if s == "":
                raise InputNormalizeError("empty value is not a valid number")
            try:
                return float(s)
            except ValueError as exc:
                raise InputNormalizeError(f"{value!r} is not a valid number") from exc
        raise InputNormalizeError(f"{type(value).__name__} is not a valid number")
    if t == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            s = value.strip().lower()
            if s in {"true", "1", "yes", "y", "on"}:
                return True
            if s in {"false", "0", "no", "n", "off"}:
                return False
            raise InputNormalizeError(f"{value!r} is not a valid boolean")
        if isinstance(value, (int, float)):
            return bool(value)
        raise InputNormalizeError(f"{type(value).__name__} is not a valid boolean")
    if t == "array":
        if isinstance(value, list):
            return value
        if value is None or value == "":
            return []
        parsed = _parse_input_literal(value) if isinstance(value, str) else value
        if isinstance(parsed, tuple):
            return list(parsed)
        if not isinstance(parsed, list):
            raise InputNormalizeError("expected a JSON/Python list")
        return parsed
    if t == "object":
        if isinstance(value, dict):
            return value
        if value is None or value == "":
            return {}
        parsed = _parse_input_literal(value) if isinstance(value, str) else value
        if not isinstance(parsed, dict):
            raise InputNormalizeError("expected a JSON/Python dict")
        return parsed
    return value


def normalize_inputs_for_fields(
    inputs: Mapping[str, Any] | None,
    fields: Mapping[str, Any] | None,
) -> dict[str, Any]:
    raw_inputs = dict(inputs or {})
    normalized = dict(raw_inputs)
    for name, field in (fields or {}).items():
        if not isinstance(field, Mapping) or name not in raw_inputs:
            continue
        try:
            normalized[name] = normalize_value_for_type(
                raw_inputs[name],
                field.get("type", "string"),
            )
        except InputNormalizeError as exc:
            raise InputNormalizeError(f"{name}: {exc}") from exc
    return normalized


def start_node_input_fields(workflow: Mapping[str, Any] | None) -> dict[str, Any]:
    for node in (workflow or {}).values():
        if isinstance(node, Mapping) and node.get("node_type") == "StartNode":
            fields = node.get("input_fields")
            return dict(fields) if isinstance(fields, Mapping) else {}
    return {}


def _validation_error_types():
    """Return the jsonschema ValidationError class as a 1-tuple for ``except``.

    lazy: keep jsonschema out of cold-import; the type is only resolved the
    first time an exception actually propagates through a wrapped call, never
    at module load time (task #483). Cached after first resolution.
    """
    global _VALIDATION_ERROR_TYPES
    if _VALIDATION_ERROR_TYPES is None:
        from jsonschema.exceptions import ValidationError
        _VALIDATION_ERROR_TYPES = (ValidationError,)
    return _VALIDATION_ERROR_TYPES


def _safe_repr(obj, depth=0):
    """Convert obj to a JSON-safe representation, replacing non-serializable objects."""
    if depth > 3:
        return "..."
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_safe_repr(v, depth + 1) for v in obj[:20]]
    if isinstance(obj, dict):
        return {str(k): _safe_repr(v, depth + 1) for k, v in list(obj.items())[:20]}
    return f"<{type(obj).__name__}>"


def safe_call_with_args(prefix: str = ""):
    """The same result envelope for sync and native async nodes.

    CancelledError deliberately propagates instead of becoming a node failure.
    """
    def safe_call(func):
        def result_for(args, kwargs):
            return {"status": "success", "output": None, "error_message": "",
                    "traceback": "", "args": _safe_repr(args),
                    "kwargs": _safe_repr(kwargs), "execution_time": -1}

        def failed(result, exc):
            result["status"] = "error"
            if isinstance(exc, _validation_error_types()):
                message = "jsonschema checked invalid, message: {}, error_path: {}, schema_path: {}".format(
                    exc.message, "->".join(map(str, exc.path)), "->".join(map(str, exc.schema_path)))
            else:
                message = str(exc)
            result["error_message"] = prefix + message if message else ""
            result["traceback"] = traceback.format_exc()

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            started = time.perf_counter()
            result = result_for(args, kwargs)
            try:
                result["output"] = func(*args, **kwargs)
            except Exception as exc:
                failed(result, exc)
            finally:
                result["execution_time"] = time.perf_counter() - started
            return result

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            started = time.perf_counter()
            result = result_for(args, kwargs)
            try:
                result["output"] = await func(*args, **kwargs)
            except Exception as exc:
                failed(result, exc)
            finally:
                result["execution_time"] = time.perf_counter() - started
            return result

        return async_wrapper if inspect.iscoroutinefunction(func) else wrapper
    return safe_call


def walk_to_scope(previous_outputs: dict, loop_stack: list) -> dict:
    """Return the output dictionary for the innermost active loop scope.

    Each stack frame owns a scratch ``scope`` for the current iteration. Body
    nodes write into it, and LoopEnd commits it to the begin node's
    ``loop_output`` only when the iteration finishes. Consequently, body nodes
    observe completed iterations only. An empty stack returns the root output
    dictionary; a malformed frame stops traversal at the last valid scope.
    """
    d = previous_outputs
    for frame in loop_stack:
        scope = frame.get("scope")
        if not isinstance(scope, dict):
            break
        d = scope
    return d


def build_scope_chain(previous_outputs: dict, loop_stack: list) -> list:
    """Build an innermost-to-root scope chain for reference resolution."""
    chain = [previous_outputs]
    for frame in loop_stack:
        scope = frame.get("scope")
        if not isinstance(scope, dict):
            break
        chain.append(scope)
    return list(reversed(chain))


def scoped_recursive_get(previous_outputs: dict, loop_stack: list, expression: str):
    """Resolve a reference from the innermost loop scope toward the root.

    A loop body can reference sibling outputs without spelling the full root
    path, while an explicit path such as
    ``LoopA.loop_output[0].CodeX.field`` can still address prior iterations.
    """
    if not expression:
        return previous_outputs

    head, _, _ = expression.partition(".")
    # Scope keys exclude brackets, so use only the leading key for lookup and
    # pass the complete expression to recursive_get after selecting a scope.
    head_key = head.split("[", 1)[0]

    chain = build_scope_chain(previous_outputs, loop_stack)
    for scope in chain:
        if isinstance(scope, dict) and head_key in scope:
            return recursive_get(scope, expression)

    raise ValueError(
        f"Reference '{expression}' could not be resolved in any scope "
        f"(innermost loop iter → root). First segment '{head_key}' not found."
    )


def recursive_get(obj, expression: str):
    """
    Extracts a value from a nested dictionary/list structure using a string expression.
    Supported formats: "node_A.result", "node_A.data.list[0]", "array[1].inner_field"
    """
    if not expression:
        return obj

    # Group 1 captures dictionary keys; group 2 captures numeric list indexes.
    pattern = re.compile(r'([^\.\[\]]+)|\[(\d+)\]')

    current = obj
    for match in pattern.finditer(expression):
        key, index = match.groups()

        try:
            if key is not None:
                # Dictionary lookup.
                if not isinstance(current, dict):
                    raise TypeError(f"Target is not a dictionary when trying to access key '{key}'.")
                if key not in current:
                    raise KeyError(key)
                current = current[key]

            elif index is not None:
                # List lookup.
                if not isinstance(current, (list, tuple)):
                    raise TypeError(f"Target is not a list when trying to access index [{index}].")
                current = current[int(index)]

        except (KeyError, IndexError):
            raise ValueError(f"Path not found: failed to resolve '{match.group(0)}' in expression '{expression}'.")

    return current
