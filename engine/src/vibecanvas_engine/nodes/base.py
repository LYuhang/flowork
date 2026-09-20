# -*- coding: utf-8 -*-
"""
BaseNode — the abstract base class for all workflow node types.

The ``trigger`` method (execution dispatcher) lives in ``trigger.py`` and is
attached to BaseNode at import time via ``nodes/__init__.py``.
"""


class BaseNode:
    """
    Base class definition for all node types in Flowork.
    A general node dictionary schema is defined in `GENERAL_NODE_SCHEMA`, which can be used for node initialization, validation, and rendering in the frontend. Please ensure to follow this schema when initializing any node.
    In addition, each specific node type can also define its own more detailed schema (especially for `node_config`) based on the general schema, you are also required to follow the specific schema when initializing a specific node.
    """
    GENERAL_NODE_SCHEMA = {
        "type": "object",
        "required": [
            "node_id",
            "node_name",
            "node_type",
            "node_description",
            "input_fields",
            "output_fields",
            "node_config",
            "children"
        ],
        "properties": {
            "node_id": {
                "type": "string",
                "pattern": "^node_\\d+$",
                "description": "Node ID must follow the pattern 'node_1', 'node_2', etc."
            },
            "node_name": {
                "type": "string",
                "description": "The node_name is a user-friendly name for this node, which can be referred by other nodes in the workflow through the format 'node_name.output_field_name' to get the output value from this node."
            },
            "node_type": {
                "type": "string",
                "enum": ["StartNode", "EndNode", "CodeNode", "PromptNode", "ParallelStartNode", "ParallelEndNode", "ConditionNode", "LoopBeginNode", "LoopEndNode", "HTTPRequestNode", "TransformNode", "TemplateNode", "TableReadNode", "TableWriteNode", "SubAgentNode"],
                "description": "The node_type indicates the specific type of this node, which determines the node's functionality and execution logic. Please choose from the predefined node types."
            },
            "node_description": {
                "type": "string",
                "description": "A brief description of the node's purpose and functionality"
            },
            "input_fields": {
                "type": "object",
                "description": "The input fields dictionary",
                "additionalProperties": {
                    "type": "object",
                    "required": [
                        "type",
                        "value",
                        "reference"
                    ],
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["string", "number", "integer", "boolean", "array", "object"]
                        },
                        "value": {
                            "description": "a preset value for this input field by user, could be of any type"
                        },
                        "reference": {
                            "type": "string",
                            "description": "a reference string to refer to previous node outputs, following the format 'node_name.output_field_name'"
                        },
                        "schema": {
                            "type": "object",
                            "description": "(optional) If the input field is of type 'object' or 'array', this schema can be used to further define the detailed structure of the input data"
                        }
                    },
                    "additionalProperties": False
                }
            },
            "output_fields": {
                "type": "object",
                "description": "The output fields dictionary, with flexible number of fields according to this nodes' functionality",
                "additionalProperties": {
                    "type": "object",
                    "required": [
                        "type",
                        "description"
                    ],
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["string", "number", "integer", "boolean", "array", "object"]
                        },
                        "description": {
                            "type": "string",
                            "description": "a description to explain the meaning of this output field"
                        },
                        "schema": {
                            "type": "object",
                            "description": "(optional) For output fields with complex data structure, this schema can be used to further define the detailed structure of the output field"
                        }
                    },
                    "additionalProperties": False
                }
            },
            "node_config": {
                "type": "object",
                "description": "As for node_config, each specific node type can define its own required fields in node_config according to its functionality. Please refer to the specific node type's schema for details."
            },
            "children": {
                "type": "array",
                "items": {
                    "type": "string"
                },
                "description": "A list of child node IDs that are directly connected to this node in the workflow graph"
            },
            "__attributes__": {
                "type": "object",
                "description": "(optional) Additional attributes for frontend canvas rendering, such as node position, color, etc.",
                "properties": {
                    "x": {
                        "type": "number"
                    },
                    "y": {
                        "type": "number"
                    }
                }
            }
        },
        "additionalProperties": False
    }

    AGENT_SPEC: dict = {}

    def __init__(self,
        node_type: str,
        node_id: str,
        node_name: str,
        node_description: str,
        input_fields: dict,
        output_fields: dict,
        node_config: dict,
        children: list,
        **kwargs
    ):
        self.node_type = node_type
        self.node_id = node_id
        self.node_name = node_name
        self.node_description = node_description
        self.input_fields = input_fields
        self.output_fields = output_fields
        self.node_config = node_config
        self.children = children
        self.__attributes__ = kwargs.get("__attributes__", None)

    def __call__(self, inputs: dict, previous_outputs: dict) -> dict:
        """
        Execute the node's functionality. This method should be overridden by subclasses.
        This function must output a dictionay, types and structures of which should meet the requirements defined in the `output_fields`.

        Args:
            inputs (dict): A dictionary containing the input values for this node execution, types and structures of which are defined in the node's input_fields. The format and structure is as follows,
                ```Json
                {
                    "input_field_name1": input_value_1,
                    "input_field_name2": input_value_2,
                    ...
                }
                ```
            previous_outputs (dict): A dictionary containing the outputs from previously executed nodes in the workflow, which can be used as context for the current node's execution.
                `previous_outputs` is stored as a map from preceeding `node_name` to node output_dict, format and structure as follows,
                ```Json
                {
                    "node_name": {
                        "output_field_1": output_field_value_1,
                        "output_field_2": output_field_value_2,
                        ...
                    },
                    ...
                }
                ```
        Returns:
            output_dict (dict): A dictionary containing the output values from this node execution, types and structures of which are defined in the node's output_fields.
                This output_dict will be stored in `previous_outputs` and could be referred by subsequent nodes in the workflow.
        """
        raise NotImplementedError("The __call__ method should be implemented by subclasses of BaseNode.")

    # trigger() is attached by nodes/__init__.py from trigger.py

    @classmethod
    def from_dict(cls, node_dict: dict):
        """Create a node instance from a dictionary."""
        return cls(**node_dict)

    def to_dict(self) -> dict:
        """Convert the node instance to a dictionary."""
        node_dict = {
            "node_type": self.node_type,
            "node_id": self.node_id,
            "node_name": self.node_name,
            "node_description": self.node_description,
            "input_fields": self.input_fields,
            "output_fields": self.output_fields,
            "node_config": self.node_config,
            "children": self.children,
        }
        if self.__attributes__ is not None:
            node_dict["__attributes__"] = self.__attributes__
        return node_dict

    @staticmethod
    def check(node_dict: dict) -> bool:
        """Check if the node's configuration is valid. This method can be overridden by subclasses to implement specific validation logic for different node types."""
        raise NotImplementedError("The check method should be implemented by subclasses of BaseNode to validate the node's configuration according to the specific requirements of each node type.")
