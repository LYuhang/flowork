# -*- coding: utf-8 -*-
"""Lightweight in-process expression sandbox for FLOW-CONTROL template eval.

The old CodeNode in-process jail is gone. CodeNode user code now runs in a
per-run subprocess pool
(:class:`vibecanvas_engine.code_runner.CodeWorkerPool`) — the selected OS sandbox is the isolation
boundary, so no in-process AST/builtins/open jail is needed for user code.

What REMAINS here is the small, controlled expression evaluator used by
``ConditionNode`` and ``TransformNode`` to evaluate template expressions like
``"{value} * 2"`` or ``"{category} == 'normal'"`` (placeholders are substituted
with ``repr(...)`` BEFORE eval). These are NOT arbitrary user programs — they are
short engine-controlled expressions over a tiny ``math``/``re`` env, so the
restricted-builtins + AST guard is a sufficient, cheap safety rail.
"""

import ast
import importlib


class SecurityError(Exception):
    """Exception raised for security violations within the sandbox."""
    pass


class PythonSandbox:
    """Restricted in-process evaluator for engine-controlled template expressions.

    Only :meth:`evaluate` is part of the live surface (ConditionNode /
    TransformNode). Each instance pre-imports a small ``libraries_config`` (e.g.
    ``{"math": "math", "re": "re"}``) into the eval env once.
    """

    def __init__(self, libraries_config=None):
        """
        :param libraries_config: Dictionary where keys are aliases in the sandbox
                                 and values are module names.
                                 Example: {"re": "re", "math": "math"}
        """
        self._libraries_config = libraries_config or {}
        # Initialize the base environment (library imports happen only once here)
        self._base_env = self._initialize_env()

    def _initialize_env(self):
        """Build the base environment dict (safe built-ins + pre-loaded libraries)."""
        env = {}

        # Restrict built-in functions (remove dangerous ones: eval, exec, open,
        # __import__, globals, etc.). This is the WHOLE builtin surface available
        # to ConditionNode / TransformNode expressions. Only pure value
        # constructors / transforms are exposed — combined with the AST guard
        # (no import / dunder access / eval/exec/open/getattr/setattr/delattr)
        # none of these provide an escape vector. Keep additions to value-only
        # helpers for the same reason.
        safe_builtins = {
            # type constructors / coercions
            'bool': bool, 'int': int, 'float': float, 'complex': complex,
            'str': str, 'bytes': bytes, 'bytearray': bytearray,
            'list': list, 'tuple': tuple, 'dict': dict,
            'set': set, 'frozenset': frozenset,
            # numeric
            'abs': abs, 'round': round, 'min': min, 'max': max, 'sum': sum,
            'divmod': divmod, 'pow': pow,
            # sequence / iteration
            'len': len, 'range': range, 'enumerate': enumerate, 'zip': zip,
            'map': map, 'filter': filter, 'sorted': sorted, 'reversed': reversed,
            'all': all, 'any': any,
            # string / formatting
            'repr': repr, 'format': format, 'chr': chr, 'ord': ord,
            'hex': hex, 'oct': oct, 'bin': bin,
            # introspection / error types usable in an expression
            'isinstance': isinstance, 'Exception': Exception, 'ValueError': ValueError,
        }
        env['__builtins__'] = safe_builtins

        for alias, mod_name in self._libraries_config.items():
            env[alias] = importlib.import_module(mod_name)

        return env

    def _security_check(self, code_str):
        """AST static check: ban imports, dunder attribute access, and direct
        calls to high-risk functions. Keeps the small template-expression
        evaluator safe without claiming to be a real isolation boundary."""
        try:
            tree = ast.parse(code_str)
        except SyntaxError as e:
            raise ValueError(f"Syntax error in code: {e}")

        banned_calls = {"eval", "exec", "open", "getattr", "setattr", "delattr"}

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                raise SecurityError("Importing new modules via 'import' is forbidden within the sandbox.")

            if isinstance(node, ast.Attribute):
                if node.attr.startswith('__') and node.attr.endswith('__'):
                    raise SecurityError(f"Access to built-in/magic attributes is forbidden within the sandbox: {node.attr}")

            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in banned_calls:
                    raise SecurityError(f"Calling high-risk functions is forbidden within the sandbox: {node.func.id}")

    def evaluate(self, expression):
        """Evaluate a single template expression in the restricted env.

        Used by ConditionNode / TransformNode AFTER they substitute field
        placeholders with ``repr(...)`` values. Environment is isolated per call
        (shallow-copy of the pre-imported base env)."""
        self._security_check(expression)

        run_env = self._base_env.copy()
        try:
            return eval(expression, run_env, run_env)
        except Exception as e:
            raise RuntimeError(f"Expression evaluation error: {e}")
