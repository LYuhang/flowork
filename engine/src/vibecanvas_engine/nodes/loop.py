# -*- coding: utf-8 -*-
"""LoopBeginNode + LoopEndNode — paired loop execution boundaries."""

from copy import deepcopy

import jsonschema

from ..utils import safe_call_with_args, recursive_get
from ..register import node_registry
from .base import BaseNode


@node_registry.register()
class LoopBeginNode(BaseNode):
    """LoopBeginNode — starts a counter loop (init/step/end); runs the body while ``i < end_value``, collecting each iteration's output into ``loop_output``. Paired with a LoopEndNode via ``loop_end_node_id``.

    Iteration state is managed by the engine (scope-aware for nested loops), not by ``__call__``.
    Authoring constraints (init/step/end shape, the loop_output/i outputs, pairing) live in ``AGENT_SPEC``.
    """
    CONFIG_SCHEMA = {
        "type": "object",
        "required": [
            "init_value",
            "step_value",
            "end_value",
            "loop_end_node_id"
        ],
        "properties": {
            "init_value": {
                "type": "object",
                "required": ["value", "reference"],
                "properties": {
                    "value": {
                        "description": "The static initial value (usually an integer) if no reference is provided."
                    },
                    "reference": {
                        "type": "string",
                        "description": "The reference path to an upstream node's output to dynamically determine the initial value."
                    }
                },
                "additionalProperties": False
            },
            "step_value": {
                "type": "integer",
                "minimum": 1,
                "description": "The step size for each iteration (must be >= 1; a step of 0 would never advance and is rejected)."
            },
            "end_value": {
                "type": "object",
                "required": ["value", "reference"],
                "properties": {
                    "value": {
                        "description": "The static end value if no reference is provided."
                    },
                    "reference": {
                        "type": "string",
                        "description": "The reference path to an upstream node's output to dynamically determine the end value."
                    }
                },
                "additionalProperties": False
            },
            "loop_end_node_id": {
                "type": ["string", "null"],
                "description": "The paired LoopEndNode id; may be null while the pair is being drafted."
            }
        },
        "additionalProperties": False
    }

    AGENT_SPEC = {
        "summary": "Start a counter loop that runs its body while i < end_value and collects each completed iteration.",
        "when_to_use": "Use when the workflow needs repeated sequential work: processing indexed items, retrying, or running a fixed/dynamic number of iterations.",
        "when_not_to_use": "For parallel processing of independent items use ParallelStartNode. For single-pass data transformation use CodeNode.",
        "constraints": [
            "The LoopBeginNode child is the first node of the loop body.",
            "Always pair it with one LoopEndNode: loop_end_node_id points to the body terminator, and the terminator points back with loop_begin_node_id.",
            "Loop control uses init_value, end_value, and step_value. If init_value.reference or end_value.reference is non-empty, that reference overrides the static value.",
            "The loop runs while i < end_value, then increments i by step_value after each completed iteration; step_value must be >= 1.",
            "output_fields must be exactly loop_output (array) and i (integer).",
            "Each loop_output item maps body node_name to that node's output object, not directly to output fields. Example: [{'multiply_item': {'doubled': 2}}, {'multiply_item': {'doubled': 4}}]. Extract results with [iteration['multiply_item']['doubled'] for iteration in loop_output]; iteration['doubled'] is incorrect. Inspect the actual loop_output before choosing a path.",
            "loop_output contains completed iteration snapshots only. During the current iteration, read current values from the body node outputs; the current iteration is appended to loop_output only when LoopEndNode is reached.",
            "When drafting, create the pair first, use loop_end_node_id=null only while the LoopEndNode is missing, then fill both pair pointers after the target nodes exist."
        ],
        "config_guide": {
            "init_value": "Object with 'value' (static int) and 'reference' (dynamic reference string). If reference is non-empty, it overrides value.",
            "step_value": "Integer >= 1. How much 'i' increments each iteration.",
            "end_value": "Object with 'value' (static int) and 'reference' (dynamic reference string). Loop exits when i >= end_value.",
            "loop_end_node_id": "The paired LoopEndNode id."
        },
        "examples": [
            {
                "scenario": "Loop 0..5 with body head wired",
                "node_dict": {
                    "node_id": "node_3",
                    "node_name": "loop_start",
                    "node_type": "LoopBeginNode",
                    "node_description": "Iterate 5 times",
                    "input_fields": {},
                    "output_fields": {
                        "loop_output": {"type": "array", "description": "Collected outputs from each iteration"},
                        "i": {"type": "integer", "description": "Current loop index"}
                    },
                    "node_config": {
                        "init_value": {"value": 0, "reference": ""},
                        "step_value": 1,
                        "end_value": {"value": 5, "reference": ""},
                        "loop_end_node_id": "node_5"
                    },
                    "children": ["node_4"],
                    "__attributes__": {"x": 200, "y": 0}
                }
            }
        ],
        "display": {
            "name": {"en": "LoopBeginNode", "zh": "循环开始节点"},
            "description": {"en": "Start a loop block with iteration control", "zh": "开始循环块，控制迭代范围"},
            "icon": "loop_begin",
            "category": {"en": "Flow Control", "zh": "流程控制"},
        }
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    @staticmethod
    @safe_call_with_args(prefix="[LoopBeginNode Check]: ")
    def check(node_dict: dict) -> bool:
        jsonschema.validate(
            instance=node_dict,
            schema=BaseNode.GENERAL_NODE_SCHEMA
        )

        specific_schema = deepcopy(BaseNode.GENERAL_NODE_SCHEMA)
        specific_schema["properties"]["node_config"] = LoopBeginNode.CONFIG_SCHEMA
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
                        "const": "LoopBeginNode"
                    },
                    "children": {
                        "type": "array",
                        "maxItems": 1
                    }
                }
            }
        )

        output_fields = node_dict.get("output_fields", {})
        assert set(output_fields.keys()) == {"loop_output", "i"}, (
            "For LoopBeginNode, output_fields must be exactly 'loop_output' "
            "and 'i'."
        )
        assert output_fields["loop_output"].get("type") == "array", (
            "For LoopBeginNode, output_fields.loop_output type must be 'array'."
        )
        assert output_fields["i"].get("type") == "integer", (
            "For LoopBeginNode, output_fields.i type must be 'integer'."
        )

    @safe_call_with_args(prefix="[LoopBeginNode Call]: ")
    def __call__(self, inputs: dict, previous_outputs: dict) -> dict:
        """Evaluate the initial loop value outside the runtime dispatcher.

        The runtime trigger owns iteration state so nested loops can keep state
        in the correct outer scope. This method remains for focused unit tests
        and external callers; it implements first entry only, not loop-back.
        """
        node_config = self.node_config
        init_val = node_config.get("init_value", {}) or {}
        ref = init_val.get("reference", "").strip() if isinstance(init_val, dict) else ""
        if ref:
            initial_i = int(recursive_get(previous_outputs, ref))
        else:
            initial_i = int(init_val.get("value", 0))
        return {
            "i": initial_i,
            "loop_output": [],
        }


@node_registry.register()
class LoopEndNode(BaseNode):
    """LoopEndNode — marks the end of a loop body; returns flow to its paired LoopBeginNode for the next iteration check. Linked back via ``loop_begin_node_id``.

    Authoring constraints (pairing, empty fields, single post-loop child) live in ``AGENT_SPEC``.
    """
    CONFIG_SCHEMA = {
        "type": "object",
        "required": [
            "loop_begin_node_id"
        ],
        "properties": {
            "loop_begin_node_id": {
                "type": ["string", "null"],
                "description": "The paired LoopBeginNode id; may be null while the pair is being drafted."
            }
        },
        "additionalProperties": False
    }

    AGENT_SPEC = {
        "summary": "Terminate one loop body iteration and return control to the paired LoopBeginNode.",
        "when_to_use": "Use as the required body terminator for a LoopBeginNode block.",
        "when_not_to_use": "Do not use standalone. For ordinary sequential flow, connect nodes directly.",
        "constraints": [
            "loop_begin_node_id must point back to the paired LoopBeginNode; it may be null only while drafting.",
            "The loop body's terminal node or branch terminals must point to this node.",
            "When this node is reached, the current iteration snapshot is appended to the paired LoopBeginNode.loop_output.",
            "Its optional child is the single node that runs after the loop exits."
        ],
        "config_guide": {
            "loop_begin_node_id": "The paired LoopBeginNode id."
        },
        "examples": [
            {
                "scenario": "Loop body terminator",
                "node_dict": {
                    "node_id": "node_5",
                    "node_name": "loop_end",
                    "node_type": "LoopEndNode",
                    "node_description": "End of loop body",
                    "input_fields": {},
                    "output_fields": {},
                    "node_config": {"loop_begin_node_id": "node_3"},
                    "children": ["node_6"],
                    "__attributes__": {"x": 400, "y": 200}
                }
            }
        ],
        "display": {
            "name": {"en": "LoopEndNode", "zh": "循环结束节点"},
            "description": {"en": "Mark end of loop body", "zh": "标记循环体结束"},
            "icon": "loop_end",
            "category": {"en": "Flow Control", "zh": "流程控制"},
        }
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    @staticmethod
    @safe_call_with_args(prefix="[LoopEndNode Check]: ")
    def check(node_dict: dict) -> bool:
        jsonschema.validate(
            instance=node_dict,
            schema=BaseNode.GENERAL_NODE_SCHEMA
        )

        specific_schema = deepcopy(BaseNode.GENERAL_NODE_SCHEMA)
        specific_schema["properties"]["node_config"] = LoopEndNode.CONFIG_SCHEMA
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
                        "const": "LoopEndNode"
                    },
                    "children": {
                        "type": "array",
                        "maxItems": 1
                    }
                }
            }
        )

        assert len(node_dict.get("input_fields", {})) == 0, "For LoopEndNode, the input_fields must be empty."
        assert len(node_dict.get("output_fields", {})) == 0, "For LoopEndNode, the output_fields must be empty."

    @safe_call_with_args(prefix="[LoopEndNode Call]: ")
    def __call__(self, inputs: dict, previous_outputs: dict) -> dict:
        return dict()
