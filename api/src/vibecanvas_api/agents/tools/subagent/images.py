"""Project tool images into user messages for vision Chat API compatibility.

The canonical graph state retains the multimodal tool result. Only the outgoing
model request is projected: some Chat APIs accept images in user messages but
not tool messages. No shared pending queue, state mutation or repeated appends.
"""
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, ToolMessage


def model_messages_with_images(messages: list) -> list:
    projected, images = [], []

    def flush():
        if images:
            projected.append(HumanMessage(content=list(images)))
            images.clear()

    for message in messages:
        if not isinstance(message, ToolMessage):
            # Keep responses to a parallel tool-call batch adjacent before
            # introducing a user message, as required by Chat APIs.
            flush()
        if isinstance(message, ToolMessage) and isinstance(message.content, list):
            image_blocks = [b for b in message.content if isinstance(b, dict) and b.get("type") == "image"]
            if image_blocks:
                text_blocks = [b for b in message.content if isinstance(b, dict) and b.get("type") == "text"]
                text = "\n".join(b.get("text", "") for b in text_blocks)
                projected.append(message.model_copy(update={"content": text or "Image tool result attached."}))
                images.extend([
                    {"type": "text", "text": f"Images from tool call {message.tool_call_id}:"},
                    *message.content,
                ])
                continue
        projected.append(message)
    flush()
    return projected


class ToolImageMessages(AgentMiddleware):
    def wrap_model_call(self, request, handler):
        return handler(request.override(messages=model_messages_with_images(request.messages)))

    async def awrap_model_call(self, request, handler):
        return await handler(request.override(messages=model_messages_with_images(request.messages)))
