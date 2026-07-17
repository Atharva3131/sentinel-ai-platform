"""ContentExtractor — converts raw document bytes to plain text by format."""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.retrieval.ingestion_exceptions import ContentExtractionError
from backend.retrieval.ingestion_models import DocumentFormat

# PDF text-stream patterns (minimal extraction without a PDF library).
# Extracts printable text from BT…ET blocks.  For production-grade PDF
# parsing, replace _extract_pdf with a pdfminer/pypdf integration.
_PDF_TEXT_BLOCK = re.compile(rb"BT(.*?)ET", re.DOTALL)
_PDF_TJ_OPERAND = re.compile(rb"\(([^)]*)\)\s*Tj")
_PDF_TJ_ARRAY = re.compile(rb"\[([^\]]*)\]\s*TJ")
_PDF_INNER_STRINGS = re.compile(rb"\(([^)]*)\)")

_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_ENTITY = re.compile(r"&[a-zA-Z]+;|&#\d+;")
_EXCESS_WHITESPACE = re.compile(r"\s{3,}")


@dataclass(slots=True)
class ContentExtractor:
    """Converts raw document bytes to a decoded text string.

    Supported formats:
        - text/plain, text/txt          → UTF-8 decode
        - text/markdown, text/md        → UTF-8 decode
        - application/json              → UTF-8 decode
        - text/html                     → tag stripping + UTF-8 decode
        - application/pdf               → minimal BT/ET stream extraction
        - unknown                       → best-effort UTF-8 decode

    PDF note: The built-in PDF extractor is intentionally minimal and works
    reliably only on simple, unencrypted PDFs with standard text streams.
    Integrate pdfminer.six or pypdf for production-grade extraction.
    """

    async def extract(
        self,
        content: bytes,
        content_type: str,
        *,
        document_id: str | None = None,
    ) -> tuple[str, DocumentFormat]:
        """Return ``(text, DocumentFormat)`` for the given raw bytes.

        Raises:
            ContentExtractionError: If decoding fails unexpectedly.
        """
        normalized = content_type.lower().strip().split(";")[0].strip()
        try:
            match normalized:
                case "application/pdf":
                    return self._extract_pdf(content), DocumentFormat.PDF
                case "text/markdown" | "text/md":
                    return content.decode("utf-8"), DocumentFormat.MARKDOWN
                case "application/json":
                    return content.decode("utf-8"), DocumentFormat.JSON
                case "text/plain" | "text/txt":
                    return content.decode("utf-8"), DocumentFormat.TEXT
                case "text/html":
                    return self._strip_html(content.decode("utf-8")), DocumentFormat.HTML
                case _:
                    return (
                        content.decode("utf-8", errors="replace"),
                        DocumentFormat.UNKNOWN,
                    )
        except ContentExtractionError:
            raise
        except Exception as exc:
            raise ContentExtractionError(
                f"Failed to extract text from content_type='{content_type}': {exc}",
                document_id=document_id,
                content_type=content_type,
            ) from exc

    def _extract_pdf(self, content: bytes) -> str:
        """Minimal BT/ET text-stream extraction — no external libraries."""
        parts: list[str] = []
        for block_match in _PDF_TEXT_BLOCK.finditer(content):
            block = block_match.group(1)
            for tj_match in _PDF_TJ_OPERAND.finditer(block):
                try:
                    parts.append(tj_match.group(1).decode("latin-1"))
                except Exception:
                    pass
            for tj_arr in _PDF_TJ_ARRAY.finditer(block):
                for s in _PDF_INNER_STRINGS.finditer(tj_arr.group(1)):
                    try:
                        parts.append(s.group(1).decode("latin-1"))
                    except Exception:
                        pass
        extracted = " ".join(parts).strip()
        if not extracted:
            # Fall back to best-effort latin-1 decode of the whole file
            extracted = content.decode("latin-1", errors="replace")
        return extracted

    def _strip_html(self, content: str) -> str:
        no_tags = _HTML_TAG.sub(" ", content)
        no_entities = _HTML_ENTITY.sub(" ", no_tags)
        return _EXCESS_WHITESPACE.sub(" ", no_entities).strip()
