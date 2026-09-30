"""Operations merge against the server draft; no full-state replacement API."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field
from .chat import ChatAttachment


class DraftOut(BaseModel):
    chat_id: str
    version: int = 0
    generation: int = 0
    text: str = ''
    attachments: list[ChatAttachment] = Field(default_factory=list)


class DraftAppend(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['append']
    operation_id: str = Field(min_length=1, max_length=128)
    attachments: list[ChatAttachment] = Field(min_length=1, max_length=32)


class DraftRemove(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['remove']
    operation_id: str = Field(min_length=1, max_length=128)
    attachment_keys: list[str] = Field(min_length=1, max_length=32)


class DraftText(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['text']
    operation_id: str = Field(min_length=1, max_length=128)
    # Compare only text, so a concurrent Preview append cannot reject typing.
    previous_text: str = Field(max_length=100000)
    text: str = Field(max_length=100000)


DraftMutation = Annotated[DraftAppend | DraftRemove | DraftText, Field(discriminator='kind')]
