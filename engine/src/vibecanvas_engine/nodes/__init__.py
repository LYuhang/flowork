# -*- coding: utf-8 -*-
"""
nodes package — all built-in node types for Flowork.

Importing this package:
  1. Attaches the ``trigger`` execution dispatcher to ``BaseNode``.
  2. Registers every concrete node class into ``node_registry`` (via
     ``@node_registry.register()`` decorators in each sub-module).
  3. Re-exports everything so that ``from .nodes import BaseNode, StartNode, ...``
     works identically to the old ``from .node import ...``.
"""

# --- base + trigger wiring ---------------------------------------------------
from .base import BaseNode
from .trigger import trigger as _trigger_impl

BaseNode.trigger = _trigger_impl

# --- config getters/setters ---------------------------------------------------
from .config import (
    set_code_timeout,
    get_code_timeout,
    set_programming_languages,
    set_prompt_models,
    get_programming_languages,
    get_prompt_models,
)

# --- registries (re-exported for backward compat with `from .node import`) ----
from ..register import node_registry, llm_registry

# --- concrete node classes (import triggers @node_registry.register()) --------
from .start import StartNode
from .end import EndNode
from .code import CodeNode
from .prompt import PromptNode
from .parallel import ParallelStartNode, ParallelEndNode
from .condition import ConditionNode
from .loop import LoopBeginNode, LoopEndNode
from .http_request import HTTPRequestNode
from .transform import TransformNode
from .template import TemplateNode
from .table_read import TableReadNode
from .table_write import TableWriteNode
from .subagent import SubAgentNode

# --- frozen pure-engine node-type snapshot (RE-6 P2 B2) ----------------------
# Captured HERE, at the END of this module, AFTER every pure node class above
# has registered into ``node_registry``. The engine package never imports api,
# so no api-defined node (e.g. ``KnowledgeSearchNode``) can be present yet —
# this snapshot is therefore guaranteed to hold ONLY the pure engine types.
#
# The host uses this to tell pure-vs-api workflows apart: the LIVE
# ``node_registry`` gets polluted once the api process imports its engine_nodes
# (registering api nodes INTO the same global), so a frozen copy is required —
# checking against the live registry would wrongly pass an api workflow that the
# engine-alone sandbox cannot run.
ENGINE_PURE_NODE_TYPES = frozenset(node_registry.list_all())

__all__ = [
    # base
    "BaseNode",
    # registries
    "node_registry",
    "llm_registry",
    # config
    "set_code_timeout",
    "get_code_timeout",
    "set_programming_languages",
    "set_prompt_models",
    "get_programming_languages",
    "get_prompt_models",
    # node classes
    "StartNode",
    "EndNode",
    "CodeNode",
    "PromptNode",
    "ParallelStartNode",
    "ParallelEndNode",
    "ConditionNode",
    "LoopBeginNode",
    "LoopEndNode",
    "HTTPRequestNode",
    "TransformNode",
    "TemplateNode",
    "TableReadNode",
    "TableWriteNode",
    "SubAgentNode",
    # frozen pure-engine snapshot
    "ENGINE_PURE_NODE_TYPES",
]
