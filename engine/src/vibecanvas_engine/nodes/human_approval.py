"""Human confirmation boundary; the owning runtime supplies the approval broker.

The engine never resolves accounts or persists decisions. Every visit (including
loop iterations and parallel branches) delegates to the runtime for a distinct
approval. Only a Boolean decision enters the workflow's data graph.
"""

from copy import deepcopy

import jsonschema

from ..register import node_registry
from ..utils import safe_call_with_args
from .base import BaseNode


@node_registry.register()
class HumanApprovalNode(BaseNode):
    CONFIG_SCHEMA = {
        "type": "object",
        "required": ["instruction", "timeout_seconds"],
        "properties": {
            "instruction": {"type": "string", "maxLength": 4000},
            "approver_email": {"type": "string", "maxLength": 254},
            "timeout_seconds": {"type": "integer", "minimum": 1},
        },
        "additionalProperties": False,
    }
    AGENT_SPEC = {
        "summary": "Wait for a person's decision, then output approved=true or false.",
        "when_to_use": "A person must authorize a downstream action.",
        "when_not_to_use": "Use ConditionNode for automated decisions. This node does not edit data.",
        "constraints": [
            "output_fields must contain only approved of type boolean; at most one child.",
            "Both approval and rejection continue to the child. Use ConditionNode to branch on approved.",
            "Timeout automatically rejects. Configure a positive timeout_seconds.",
            "approver_email selects a registered user. Omit for the workflow/task initiator; deployments require an explicit email.",
            "Every visit creates a separate approval, including visits inside loops and parallel branches.",
        ],
        "config_guide": {
            "instruction": "Plain text describing what the reviewer must confirm.",
            "approver_email": "Email of the designated reviewer; authorization uses the resolved account identity.",
            "timeout_seconds": "Positive integer seconds to wait before automatic rejection.",
        },
        "examples": [
            {
                "scenario": "Wait for the workflow initiator; branch on review.approved downstream",
                "node_dict": {
                    "node_id": "node_2",
                    "node_name": "review",
                    "node_type": "HumanApprovalNode",
                    "node_description": "Confirm before continuing",
                    "input_fields": {},
                    "output_fields": {"approved": {"type": "boolean", "description": "Reviewer decision"}},
                    "node_config": {"instruction": "Confirm this action may proceed.", "timeout_seconds": 3600},
                    "children": [],
                },
            }
        ],
        "display": {
            "name": {"en": "Human approval", "zh": "人工确认"},
            "description": {"en": "Wait for approval or rejection", "zh": "等待人工通过或驳回，超时自动驳回"},
            "icon": "condition",
            "category": {"en": "Flow Control", "zh": "流程控制"},
        },
    }

    @staticmethod
    @safe_call_with_args(prefix="[HumanApprovalNode Check]: ")
    def check(node_dict: dict) -> bool:
        schema = deepcopy(BaseNode.GENERAL_NODE_SCHEMA)
        schema["properties"]["node_config"] = HumanApprovalNode.CONFIG_SCHEMA
        jsonschema.validate(node_dict, schema)
        assert node_dict["node_type"] == "HumanApprovalNode"
        assert len(node_dict["children"]) <= 1, "Human approval permits at most one child"
        outputs = node_dict["output_fields"]
        assert set(outputs) == {"approved"}, "Human approval outputs only 'approved'"
        assert outputs["approved"].get("type") == "boolean", "approved must be boolean"

    async def call_async(self, inputs: dict, previous_outputs: dict, extra: dict) -> dict:
        broker = extra.get("human_approval")
        if broker is None:
            return {
                "status": "error",
                "output": None,
                "traceback": "",
                "error_message": "This execution runtime does not support human approval",
            }
        try:
            approved = await broker(self, inputs)
            if type(approved) is not bool:
                raise TypeError("Approval broker must return a boolean")
            return {"status": "success", "output": {"approved": approved}, "error_message": "", "execution_time": 0.0}
        except Exception as exc:
            return {"status": "error", "output": None, "traceback": "", "error_message": str(exc)}
