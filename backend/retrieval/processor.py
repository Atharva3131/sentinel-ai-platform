"""DocumentProcessor — content validation, decoding, and normalization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from backend.retrieval.exceptions import DocumentProcessingError
from backend.retrieval.models import Document, DocumentMetadata

_DEFAULT_MAX_BYTES: int = 10 * 1024 * 1024  # 10 MiB
_DEFAULT_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/html",
        "application/json",
        "application/xml",
        "text/xml",
    }
)


@dataclass(slots=True)
class DocumentProcessor:
    """Validates, decodes, and normalizes raw document content into a Document.

    ``max_bytes`` caps the accepted content size to prevent runaway ingestion.
    ``supported_content_types`` is the allowlist for the ``content_type`` field in
    DocumentMetadata; a None content_type bypasses the check.
    """

    max_bytes: int = _DEFAULT_MAX_BYTES
    supported_content_types: frozenset[str] = field(
        default_factory=lambda: _DEFAULT_CONTENT_TYPES
    )

    async def process(
        self,
        content: bytes | str,
        metadata: DocumentMetadata,
    ) -> Document:
        """Validate, decode, and return a Document.

        Raises:
            DocumentProcessingError: If the content exceeds ``max_bytes``,
                uses an unsupported content type, or cannot be decoded as UTF-8.
        """
        raw_bytes = content if isinstance(content, bytes) else content.encode("utf-8")

        if len(raw_bytes) > self.max_bytes:
            raise DocumentProcessingError(
                f"Document '{metadata.document_id}' exceeds max size "
                f"({len(raw_bytes)} > {self.max_bytes} bytes)",
                document_id=metadata.document_id,
                content_type=metadata.content_type,
            )

        if (
            metadata.content_type is not None
            and metadata.content_type not in self.supported_content_types
        ):
            raise DocumentProcessingError(
                f"Unsupported content type '{metadata.content_type}' for "
                f"document '{metadata.document_id}'",
                document_id=metadata.document_id,
                content_type=metadata.content_type,
            )

        text = self._decode(raw_bytes, metadata)
        text = self._normalize(text, metadata)
        checksum = self._checksum(raw_bytes)

        from dataclasses import replace

        enriched_metadata = replace(
            metadata,
            byte_size=len(raw_bytes),
            checksum=checksum,
        )

        return Document(
            document_id=metadata.document_id,
            content=text,
            metadata=enriched_metadata,
        )

    def _decode(self, raw_bytes: bytes, metadata: DocumentMetadata) -> str:
        try:
            return raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DocumentProcessingError(
                f"Document '{metadata.document_id}' is not valid UTF-8: {exc}",
                document_id=metadata.document_id,
                content_type=metadata.content_type,
            ) from exc

    def _normalize(self, text: str, metadata: DocumentMetadata) -> str:
        if metadata.content_type == "application/json":
            try:
                parsed = json.loads(text)
                return json.dumps(parsed, sort_keys=True, default=str)
            except json.JSONDecodeError:
                return text
        return text.replace("\r\n", "\n").replace("\r", "\n")

    def _checksum(self, raw_bytes: bytes) -> str:
        return hashlib.sha256(raw_bytes).hexdigest()[:16]
