# -*- coding: utf-8 -*-
"""CodeNode — executes a custom code snippet in a per-run worker subprocess."""

import os
import jsonschema

from copy import deepcopy

from ..utils import safe_call_with_args
from ..code_runner import CodeWorkerPool
from ..register import node_registry
from .base import BaseNode
from .config import _CODE_LANGUAGES, get_code_timeout


@node_registry.register()
class CodeNode(BaseNode):
    """CodeNode — runs a user-provided code snippet in the selected programming language (Python today; more may be added) inside a per-run worker subprocess, and returns its output dict.

    Authoring constraints (the entry-point shape, the import model, timeout) live in ``AGENT_SPEC``.
    """
    # Class-level fallback so ``__new__``-constructed instances (used in some
    # tests) still resolve a timeout. ``__init__`` shadows it per-instance and
    # ``Workflow.__init__`` may override that from ``__meta__.settings``.
    _default_timeout: float = get_code_timeout()

    # CodeNode runs user code in a per-run subprocess pool, which blocks the
    # calling thread on a pipe read. The engine dispatcher
    # (``nodes/exec.py``) honors this flag by running ``__call__`` off the event
    # loop via ``asyncio.to_thread`` — so the blocking pool wait never stalls the
    # loop and parallel branches can run concurrently on distinct workers.
    REQUIRES_THREAD_BRIDGE: bool = True

    CONFIG_SCHEMA = {
        "type": "object",
        "required": [
            "programming_language",
            "process_fn"
        ],
        "properties": {
            "programming_language": {
                "type": "string",
                "enum": _CODE_LANGUAGES,
                "description": "The programming language of the code snippet."
            },
            "process_fn": {
                "type": "string",
                "description": (
                    "Source code for the selected language. For Python: a single "
                    "top-level `def process_fn(inputs):` that returns a dict matching "
                    "output_fields. The runner extracts `process_fn` by name, "
                    "so the function must be named exactly that and take one "
                    "positional dict argument. The standard library is always "
                    "importable; third-party packages must be among the libraries "
                    "currently declared available for this workflow."
                )
            },
        },
        "additionalProperties": False
    }

    AGENT_SPEC = {
        "summary": "Execute custom code that takes an inputs dict and returns an output dict. Set Code timeout in Workflow Settings (__meta__.settings.timeouts.code), not node_config.",
        "when_to_use": (
            "Use for deterministic transformation, parsing, validation, scoring, "
            "filtering, merging, formatting, or adapting complex inputs for "
            "downstream nodes. A common pattern is CodeNode -> PromptNode: use "
            "CodeNode to compose nested case data, many fields, or multimodal "
            "references into one prompt-ready text field, then let PromptNode "
            "combine that field with reasoning instructions."
        ),
        "when_not_to_use": "Use PromptNode for LLM reasoning/generation. Use ConditionNode for branch routing.",
        "constraints": [
            "For Python, process_fn must be a top-level `def process_fn(inputs):`; read inputs by field name and return a JSON-serializable dict whose keys match output_fields.",
            "Do not write import statements inside process_fn; basic and workflow-declared libraries are already exposed by the execution environment.",
            "Before a PromptNode, use CodeNode when the case input is nested, has many fields, or contains multimodal references: build a readable prompt-ready string output such as `prompt_case`, then reference that string from the PromptNode.",
            "For run-local file exchange, use normal relative paths with `open(...)`; they resolve in the run working directory."
        ],
        "config_guide": {
            "process_fn": (
                "For Python: source with a single top-level `def process_fn(inputs):` "
                "returning a dict whose keys match output_fields. See constraints "
                "for the import model."
            ),
        },
        "examples": [
            {
                "scenario": "Compose a prompt-ready text block with image placeholders",
                "node_dict": {
                    "node_id": "node_3",
                    "node_name": "compose_prompt_case",
                    "node_type": "CodeNode",
                    "node_description": "Prepare a text case block for a downstream PromptNode",
                    "input_fields": {
                        "input_text": {"type": "string", "value": "", "reference": "__start__.input_text"},
                        "input_images": {"type": "array", "value": [], "reference": "__start__.input_images"},
                        "input_info": {"type": "object", "value": {}, "reference": "__start__.input_info"}
                    },
                    "output_fields": {
                        "prompt_case": {"type": "string", "description": "Prompt-ready text block with multimodal placeholders"}
                    },
                    "node_config": {
                        "programming_language": "python",
                        "process_fn": "def process_fn(inputs):\n    info = inputs.get('input_info') or {}\n    lines = [\n        '# Case',\n        f\"Text: {inputs.get('input_text', '')}\",\n        f\"Source: {info.get('source', '')}\",\n        f\"Locale: {info.get('locale', '')}\",\n        '',\n        '# Images',\n    ]\n    for image in inputs.get('input_images') or []:\n        lines.append(f'[<<image>>]({image})')\n    return {'prompt_case': '\\n'.join(lines)}"
                    },
                    "children": ["node_4"],
                    "__attributes__": {"x": 200, "y": 0}
                }
            }
        ],
        "display": {
            "name": {"en": "CodeNode", "zh": "代码节点"},
            "description": {"en": "Run code to process data", "zh": "运行代码处理数据"},
            "icon": "code",
            "category": {"en": "Data Processing", "zh": "数据处理"},
        }
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Third-party libs are NOT injected here — the worker imports them from
        # the content-addressed overlay in ``VC_LIB_OVERLAY`` (appended after
        # stdlib during worker bootstrap),
        # provisioned from ``__meta__.settings.code_requirements``. The engine
        # injects nothing.
        # Workflow Settings v1 (#484): the per-workflow default CodeNode timeout.
        # ``Workflow.__init__`` overrides this from ``__meta__.settings.timeouts.code``.
        # Legacy node-level timeout values no longer override Workflow Settings.
        self._default_timeout: float = get_code_timeout()

    @staticmethod
    @safe_call_with_args(prefix="[CodeNode Check]: ")
    def check(node_dict: dict) -> bool:
        # Old saved graphs may carry the retired override. Accept them without
        # mutating history; execution uses Workflow Settings exclusively.
        node_dict = deepcopy(node_dict)
        if isinstance(node_dict.get("node_config"), dict):
            node_dict["node_config"].pop("timeout", None)
        jsonschema.validate(
            instance=node_dict,
            schema=BaseNode.GENERAL_NODE_SCHEMA
        )

        specific_schema = deepcopy(BaseNode.GENERAL_NODE_SCHEMA)
        specific_schema["properties"]["node_config"] = CodeNode.CONFIG_SCHEMA
        jsonschema.validate(
            instance=node_dict,
            schema=specific_schema
        )

        jsonschema.validate(
            instance=node_dict,
            schema={
                "type": "object",
                "properties": {
                    "node_type": {
                        "const": "CodeNode"
                    },
                    "children": {
                        "type": "array",
                        "maxItems": 1
                    }
                }
            }
        )

        pass

    @staticmethod
    def _dependency_pythonpath(extra: dict) -> str:
        """Return custom Workflow packages followed by the platform base set.

        Both tiers are appended only after the stdlib by ``code_worker``. A
        custom requirement therefore overrides a base third-party package,
        while neither tier can shadow Python's standard library.
        """
        candidates = [
            extra.get("code_pythonpath"),
            os.environ.get("VC_LIB_OVERLAY"),
            os.environ.get("VC_SANDBOX_PYTHON_PATHS"),
        ]
        paths: list[str] = []
        for raw in candidates:
            if not isinstance(raw, str):
                continue
            for path in raw.split(os.pathsep):
                path = path.strip()
                if path and path not in paths:
                    paths.append(path)
        return os.pathsep.join(paths)

    @staticmethod
    def _get_run_pool(extra: dict) -> CodeWorkerPool:
        """Get-or-create the run's shared :class:`CodeWorkerPool`, stashed on
        ``extra["_code_pool"]``.

        Lazily created ONCE per run with ``cwd = extra["run_dir"] or os.getcwd()``
        and dependency path = the Workflow's content-addressed overlay followed
        by the explicitly mounted platform base package paths. The subprocess
        starts with ``PYTHONPATH=""`` and appends both tiers only after stdlib
        bootstrap, so neither can shadow stdlib names. CodeNode calls may arrive
        concurrently on distinct
        ``asyncio.to_thread`` threads (parallel branches), so creation is guarded
        by the engine-provided ``extra["thread_lock"]`` when present; the
        single-node ``run_node`` path passes a bare ``{}`` (no concurrency) and
        falls through unlocked. Teardown of the pool is the NEXT task (3a-3); here
        the engine does a best-effort close at run end.
        """
        pool = extra.get("_code_pool")
        if pool is not None:
            return pool
        lock = extra.get("thread_lock")
        if lock is not None:
            with lock:
                pool = extra.get("_code_pool")
                if pool is None:
                    pool = CodeWorkerPool(
                        pythonpath=CodeNode._dependency_pythonpath(extra),
                        cwd=extra.get("run_dir") or os.getcwd(),
                    )
                    extra["_code_pool"] = pool
                return pool
        pool = CodeWorkerPool(
            pythonpath=CodeNode._dependency_pythonpath(extra),
            cwd=extra.get("run_dir") or os.getcwd(),
        )
        extra["_code_pool"] = pool
        return pool

    @safe_call_with_args(prefix="[CodeNode Call]: ")
    def __call__(self, inputs: dict, previous_outputs: dict, extra: dict = None) -> dict:
        """Run ``process_fn`` in the run's worker pool (off-loop via the thread
        bridge — see ``REQUIRES_THREAD_BRIDGE``).

        ``CodeWorkerPool.run`` returns an envelope: ``{"status": "success",
        "output": {...}}`` on success or ``{"status": "error", "error_message",
        ...}`` on a user error / timeout / worker crash. We RECONCILE that with
        ``safe_call_with_args`` (which wraps THIS method's return value into a
        node-result envelope and records any raised exception as a node error):

          * success → return the user dict, so the outer envelope's
            ``status=="success"`` and ``output`` IS the user dict.
          * error envelope → RAISE so the outer wrapper records it as a normal
            ``error_dict[node_id]`` entry (transport unchanged). The worker's
            timeout/crash thus becomes a regular node error, not a hard crash.
        """
        extra = extra or {}
        code_str = self.node_config["process_fn"]
        timeout = float(self._default_timeout)

        pool = self._get_run_pool(extra)
        envelope = pool.run(code_str, inputs, timeout)

        if envelope.get("status") == "success":
            return envelope.get("output")
        raise RuntimeError(envelope.get("error_message") or "CodeNode execution failed")
