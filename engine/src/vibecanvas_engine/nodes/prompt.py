# -*- coding: utf-8 -*-
"""PromptNode — formats inputs into a prompt and calls an LLM."""

import os
import re
import json
import jsonschema
from copy import deepcopy
from json_repair import repair_json, loads

from ..utils import safe_call_with_args
from ..register import node_registry
from .base import BaseNode


@node_registry.register()
class PromptNode(BaseNode):
    """PromptNode — formats inputs into a prompt, calls an LLM, and parses its JSON response into output_fields. Supports text + multimodal (image/video/audio) inputs.

    Authoring constraints (template/model/inference_config, the multimodal + interpolation syntax) live in ``AGENT_SPEC``.
    """
    # Engine dispatch uses call_async; the sync entry is for direct callers.


    CONFIG_SCHEMA = {
        "type": "object",
        "required": [
            "prompt_template",
            "model_name",
            "inference_config"
        ],
        "properties": {
            "prompt_template": {
                "type": "string",
                "description": "The prompt template string, supporting multimodality slots (e.g., [<<image>>](url)) and variable interpolation (e.g., {{field_name}})."
            },
            "model_name": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "The exact models key returned by "
                    "flowork-cli config get --scope model_api. Before authoring this node, call "
                    "that command in the current build turn and copy one enabled key "
                    "verbatim. Never use the chat Agent's own model id, a provider "
                    "model id, or a guessed model name. If no model is returned, do "
                    "not create a PromptNode; ask the user to configure an API model."
                )
            },
            "inference_config": {
                "type": "object",
                "required": [
                    "temperature",
                    "max_tokens",
                    "top_k",
                    "top_p"
                ],
                "properties": {
                    "temperature": {
                        "type": "number",
                        "minimum": 0,
                        "description": "Sampling temperature."
                    },
                    "max_tokens": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Generation token budget. Some providers count reasoning "
                            "tokens against this limit as well as visible output; "
                            "allow room for both even when the requested JSON is short."
                        )
                    },
                    "top_k": {
                        "type": "integer",
                        "minimum": -1,
                        "description": "Top-k sampling parameter."
                    },
                    "top_p": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": "Nucleus sampling probability (top-p)."
                    },
                    "extra_body": {
                        "type": ["object", "string"],
                        "description": (
                            "(Optional) Provider-specific request-body extras "
                            "forwarded to the OpenAI-SDK client as extra_body "
                            "(e.g. {\"reasoning_effort\": \"high\"}). Accepts a JSON "
                            "object or a JSON-object string; a blank/unparseable "
                            "value is ignored at runtime."
                        )
                    }
                },
                "additionalProperties": False
            },
            "custom_model_config": {
                "type": "object",
                "description": (
                    "Deprecated legacy field; do not author it. Select a manually "
                    "added API by model_name. Inline credentials are rejected and "
                    "connection overrides are not supported. Remove this field "
                    "when replacing a legacy model."
                ),
                "properties": {
                    "model_name": {
                        "type": "string",
                        "description": "Legacy field; not used for Workflow model resolution."
                    },
                    "api_key": {
                        "type": "string",
                        "description": "Forbidden inline credential; never store an API key in a Workflow."
                    },
                    "api_url": {
                        "type": "string",
                        "description": "Legacy field; configure the endpoint in API Management instead."
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Legacy field; not used for Workflow model resolution."
                    }
                }
            }
        },
        "additionalProperties": False
    }

    AGENT_SPEC = {
        "summary": "Format inputs into a prompt, call an LLM, and parse the JSON response into output fields.",
        "when_to_use": (
            "Use for LLM inference: classification, extraction, generation, "
            "summarization, semantic analysis, and multimodal understanding. "
            "For complex case inputs, pair it with a preceding CodeNode: CodeNode "
            "composes the case into a prompt-ready text field, then PromptNode "
            "uses instructions plus that field for reasoning."
        ),
        "when_not_to_use": "Use CodeNode for deterministic transformation or for composing complex case inputs before a PromptNode. Use ConditionNode for branch routing.",
        "constraints": [
            "input_fields must be primitive types (string, number, integer, or boolean); use CodeNode first to compose array/object data into a prompt-ready string.",
            "Mandatory model-discovery gate: in the current build turn, call flowork-cli config get --scope model_api before writing any PromptNode, then copy one enabled models key exactly into model_name.",
            "Never use the chat Agent's runtime model id, a provider model id, or a guessed/familiar model name. If global config returns no model, do not create this node; ask the user to configure an API model first.",
            "prompt_template must include a JSON output format block with quoted keys matching every output_fields key.",
            "For nested dictionaries/lists, many case fields, or multimodal references, use a preceding CodeNode to compose one readable prompt-ready text field such as `prompt_case`; reference it in prompt_template with {{prompt_case}}."
        ],
        "config_guide": {
            "prompt_template": (
                "Markdown prompt sent to the LLM. Prefer readable sections: # Task, # Input, # Instructions or Rubric, optional # Examples, and mandatory # Output Format. "
                "Keep business rules close to the relevant instruction section so users can inspect and edit the prompt easily. "
                "# Output Format must require a JSON object/dict whose quoted keys match output_fields. "
                "{{field_name}} is replaced at runtime; use primitive input fields only. If the source case is nested, has many fields, or includes multimodal references, first use CodeNode to build a prompt-ready string field (for example `prompt_case`), then place {{prompt_case}} in the # Input section. Unknown names remain literal. "
                "Embed media with [<<image>>](url_or_path), [<<video>>](url_or_path), or [<<audio>>](url_or_path); the URL/path may also use {{field}} interpolation."
            ),
            "model_name": "Exact enabled key from flowork-cli config get --scope model_api. Fetch it in this build turn and copy it verbatim; never guess or substitute the Agent runtime model.",
            "inference_config": "Object with temperature (float), max_tokens (int), top_k (int), top_p (float) controlling generation."
        },
        "examples": [
            {
                "scenario": "Multimodal image description",
                "node_dict": {
                    "node_id": "node_3",
                    "node_name": "image_describer",
                    "node_type": "PromptNode",
                    "node_description": "Describe an image using multimodal LLM",
                    "input_fields": {
                        "image_url": {"type": "string", "value": "", "reference": "__start__.image_url"},
                        "question": {"type": "string", "value": "", "reference": "__start__.question"}
                    },
                    "output_fields": {
                        "description": {"type": "string", "description": "Image description"},
                        "answer": {"type": "string", "description": "Answer to the question"}
                    },
                    "node_config": {
                        "prompt_template": "# Task\nLook at this image: [<<image>>]({{image_url}})\n\nAnswer the following question about it:\n{{question}}\n\n# Output Format\n```json\n{\"description\": \"[brief image description]\", \"answer\": \"[your answer]\"}\n```",
                        "model_name": "<manual-api-name>",
                        "inference_config": {"temperature": 0.3, "max_tokens": 512, "top_k": -1, "top_p": 0.95}
                    },
                    "children": ["node_4"],
                    "__attributes__": {"x": 200, "y": 0}
                }
            }
        ],
        "display": {
            "name": {"en": "PromptNode", "zh": "提示词节点"},
            "description": {"en": "Call LLM with formatted prompt and parse JSON response", "zh": "使用格式化提示词调用大模型并解析 JSON 响应"},
            "icon": "prompt",
            "category": {"en": "AI Inference", "zh": "AI 推理"},
        }
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    @staticmethod
    @safe_call_with_args(prefix="[PromptNode Check]: ")
    def check(node_dict: dict) -> bool:
        jsonschema.validate(
            instance=node_dict,
            schema=BaseNode.GENERAL_NODE_SCHEMA
        )

        specific_schema = deepcopy(BaseNode.GENERAL_NODE_SCHEMA)
        specific_schema["properties"]["node_config"] = PromptNode.CONFIG_SCHEMA
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
                        "const": "PromptNode"
                    },
                    "children": {
                        "type": "array",
                        "maxItems": 1
                    }
                }
            }
        )

        for field_name, field_info in node_dict["input_fields"].items():
            field_type = field_info.get("type")
            assert field_type not in {"array", "object"}, (
                f"PromptNode input field '{field_name}' has nested type "
                f"'{field_type}'. PromptNode does not support list/array or "
                f"object input fields for prompt interpolation. Use a preceding "
                f"CodeNode to convert complex nested structures into a "
                f"prompt-ready string field, then pass that string field to "
                f"PromptNode."
            )

        # Every output field must appear as a quoted JSON key in the prompt
        # template's output-format section, so the LLM is actually told to
        # produce it (and json_repair can map it back to the field). We match
        # the field name wrapped in double or single quotes, e.g. "score" or
        # 'score'.
        prompt_template = node_dict["node_config"]["prompt_template"]
        for field_name in node_dict["output_fields"]:
            assert (
                f'"{field_name}"' in prompt_template
                or f"'{field_name}'" in prompt_template
            ), (
                f"For PromptNode, every output field must be referenced in the "
                f"prompt_template's output-format section as a quoted key, but "
                f"output field '{field_name}' was not found (expected "
                f"\"{field_name}\" or '{field_name}' in the template)."
            )

    @staticmethod
    def format_prompt_template(prompt_template: str, inputs: dict, unpack_multimodal: bool = True):
        """Parses the prompt template, interpolates variables, and extracts multimodal elements."""
        IMAGE_PLACEHOLDER = os.environ.get("IMAGE_PLACEHOLDER", "<<image>>")
        VIDEO_PLACEHOLDER = os.environ.get("VIDEO_PLACEHOLDER", "<<video>>")
        AUDIO_PLACEHOLDER = os.environ.get("AUDIO_PLACEHOLDER", "<<audio>>")

        def replace_var(match):
            var_name = match.group(1).strip()
            if var_name in inputs:
                val = inputs[var_name]
                return json.dumps(val, ensure_ascii=False) if isinstance(val, (dict, list)) else str(val)
            return match.group(0)

        interpolated_prompt = re.sub(r"\{\{(.*?)\}\}", replace_var, prompt_template)

        image_list, video_list, audio_list = [], [], []

        pattern = rf"\[({IMAGE_PLACEHOLDER}|{VIDEO_PLACEHOLDER}|{AUDIO_PLACEHOLDER})\]\((.*?)\)"

        def process_multimodal(match):
            placeholder = match.group(1)
            url_or_path = match.group(2).strip()

            if unpack_multimodal:
                if placeholder == IMAGE_PLACEHOLDER:
                    image_list.append(url_or_path)
                elif placeholder == VIDEO_PLACEHOLDER:
                    video_list.append(url_or_path)
                elif placeholder == AUDIO_PLACEHOLDER:
                    audio_list.append(url_or_path)
                # Leave the BARE placeholder inline (ShareGPT convention) so the
                # provider's convert_input splits the text on it and interleaves
                # the media at the right position (model_utils.convert_input).
                return placeholder
            else:
                return url_or_path

        final_prompt = re.sub(pattern, process_multimodal, interpolated_prompt)

        return final_prompt, image_list, video_list, audio_list

    @staticmethod
    def _build_injected_model(entry: dict):
        """Build a provider client from an injected tenant-credential ``entry``.

        ``entry`` is one value of ``extra['llm_credentials']`` — a dict the api
        assembled server-side from a saved ``llm_credentials`` row:
        ``{provider, model_name, api_url, api_key, timeout?}``. ``provider`` is
        mapped to the matching ``BaseLLM`` subclass in ``custom_llms``. Routing
        precedence:
          1. One of the 4 CANONICAL provider ids (case-insensitive):
             ``openai`` → OpenAIModel, ``azure_openai`` → AzureOpenAIModel,
             ``anthropic`` → AnthropicModel, ``google_genai`` → GoogleGenaiModel.
          2. An exact CUSTOM_PROVIDERS match (legacy inline 'OpenAI'/'Gemini').
          3. Family fallback: 'gemini'/'google' → GeminiModel.
          4. OpenAI-compatible default (DeepSeek/Moonshot/Qwen/etc.).
        The api_key is read here and never echoed back out.
        """
        from .. import custom_llms
        from ..custom_llms import (
            CANONICAL_PROVIDERS, CUSTOM_PROVIDERS, OpenAIModel, GeminiModel,
        )

        provider_name = (entry.get("provider") or "").strip()
        canonical = CANONICAL_PROVIDERS.get(provider_name.lower())
        if canonical is not None:
            # Resolve the class by name off the live module so monkeypatched
            # classes (in tests) are honored, not the load-time snapshot.
            klass = getattr(custom_llms, canonical["class_name"], canonical["class"])
            default_url = canonical["default_url"]
            default_model = canonical["default_model"]
        elif provider_name in CUSTOM_PROVIDERS:
            klass = CUSTOM_PROVIDERS[provider_name]["class"]
            default_url = CUSTOM_PROVIDERS[provider_name]["default_url"]
            default_model = CUSTOM_PROVIDERS[provider_name]["default_model"]
        elif provider_name.lower() in ("gemini", "google"):
            klass = GeminiModel
            default_url = CUSTOM_PROVIDERS["Gemini"]["default_url"]
            default_model = CUSTOM_PROVIDERS["Gemini"]["default_model"]
        else:
            # OpenAI-compatible default (OpenAI, DeepSeek, Moonshot, Qwen, …).
            klass = OpenAIModel
            default_url = CUSTOM_PROVIDERS["OpenAI"]["default_url"]
            default_model = CUSTOM_PROVIDERS["OpenAI"]["default_model"]

        kwargs = dict(
            model_name=entry.get("model_name") or default_model,
            api_key=entry.get("api_key", ""),
            api_url=entry.get("api_url") or default_url,
            timeout=int(entry.get("timeout") or 60),
        )
        # Optional outbound proxy — only the OpenAI-compatible / Azure clients
        # accept it (httpx http_client). Anthropic / Gemini ctors don't, so omit
        # it there (pass it only when the target class declares a ``proxy`` param).
        proxy = entry.get("proxy")
        if proxy:
            import inspect
            try:
                if "proxy" in inspect.signature(klass.__init__).parameters:
                    kwargs["proxy"] = proxy
            except (TypeError, ValueError):  # pragma: no cover - defensive
                pass
        return klass(**kwargs)

    def _prepare_call(self, inputs, extra):
        stop_event = (extra or {}).get("stop_event")
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError("PromptNode cancelled before model call.")

        prompt_template = self.node_config["prompt_template"]

        formatted_text, images, videos, audios = self.format_prompt_template(
            prompt_template=prompt_template,
            inputs=inputs,
            unpack_multimodal=True
        )

        # Media paths are already real inside the sandbox (`/run` is temporary;
        # `/mount` is durable); http(s) URLs and other strings
        # pass through unchanged. The provider opens them directly.
        conversation_dict = {
            "conversations": [
                {"from": "human", "value": formatted_text}
            ],
            "image": images,
            "video": videos,
            "audio": audios
        }

        model_name = self.node_config["model_name"]
        inference_config = self.node_config.get("inference_config", {})
        # Resolution precedence:
        #   (1) An injected mapping rides ambiently on ``extra`` under
        #       ``llm_credentials``. Its api_key is a short-lived host-broker
        #       capability and api_url is the internal broker, never a provider
        #       credential or user endpoint.
        # No registered-model or inline-credential fallback is allowed.
        injected = (extra or {}).get("llm_credentials") or {}
        try:
            if model_name in injected:
                model = self._build_injected_model(injected[model_name])
            else:
                raise RuntimeError(
                    "Workflow model is unavailable; select your enabled manually "
                    "added API. Platform defaults and account connections are not supported."
                )
        except Exception as e:
            raise RuntimeError(f"Failed to initialize model '{model_name}': {str(e)}")

        return model, conversation_dict, inference_config, stop_event

    def _parse_output(self, raw_output):
        if not isinstance(raw_output, str) or not raw_output.strip():
            raise ValueError(
                "The model returned no text. Increase the node's max_tokens "
                "or select a model that can complete the requested JSON output."
            )

        try:
            parsed_output = loads(repair_json(raw_output))
        except Exception as e:
            raise ValueError(f"Failed to parse LLM output into a JSON dictionary. Error: {str(e)}\nRaw Model Output:\n{raw_output}")

        if not isinstance(parsed_output, dict):
            raise ValueError(
                "The model output must be a JSON object matching the node's "
                f"declared output fields, but received {type(parsed_output).__name__}."
            )

        declared_fields = set(getattr(self, "output_fields", {}) or {})
        missing_fields = sorted(declared_fields.difference(parsed_output))
        if missing_fields:
            raise ValueError(
                "The model output is missing declared field(s): "
                + ", ".join(missing_fields)
                + "."
            )
        return parsed_output

    @safe_call_with_args(prefix="[PromptNode Call]: ")
    def __call__(self, inputs: dict, previous_outputs: dict, extra: dict = None) -> dict:
        model, conversation, config, stop = self._prepare_call(inputs, extra)
        try:
            raw = model(conversation, config, stop_event=stop)
        except Exception as exc:
            raise RuntimeError(f"LLM generation failed: {exc}") from exc
        if stop is not None and stop.is_set():
            raise RuntimeError("PromptNode cancelled after model call; output discarded.")
        return self._parse_output(raw)

    @safe_call_with_args(prefix="[PromptNode Call]: ")
    async def call_async(self, inputs: dict, previous_outputs: dict, extra: dict = None) -> dict:
        model, conversation, config, stop = self._prepare_call(inputs, extra)
        try:
            raw = await model.acall(conversation, config, stop_event=stop)
        except Exception as exc:
            raise RuntimeError(f"LLM generation failed: {exc}") from exc
        if stop is not None and stop.is_set():
            raise RuntimeError("PromptNode cancelled after model call; output discarded.")
        return self._parse_output(raw)
