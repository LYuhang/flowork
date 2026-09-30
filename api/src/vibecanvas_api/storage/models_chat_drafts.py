"""Private draft payloads; operation receipts contain no user content."""
from sqlalchemy import BigInteger, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
import uuid
from .models import Base


class ChatContextDraft(Base):
    __tablename__ = 'chat_context_drafts'
    chat_id: Mapped[str] = mapped_column(Text, ForeignKey('chats.chat_id', ondelete='CASCADE'), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey('tenants.tenant_id'), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0", nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0", nullable=False)
    content_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    content_nonce: Mapped[str] = mapped_column(Text, nullable=False)
    content_key_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey('content_encryption_keys.key_id', ondelete='RESTRICT'), nullable=False)


class ChatContextDraftOperation(Base):
    __tablename__ = 'chat_context_draft_operations'
    chat_id: Mapped[str] = mapped_column(Text, ForeignKey('chats.chat_id', ondelete='CASCADE'), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey('tenants.tenant_id'), nullable=False)
    operation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    request_digest: Mapped[str] = mapped_column(Text, nullable=False)
    applied_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
