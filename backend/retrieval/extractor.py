"""MetadataExtractor — parses structured metadata from document content."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from backend.retrieval.ingestion_exceptions import MetadataExtractionError
from backend.retrieval.ingestion_models import ExtractedMetadata

_MARKDOWN_H1 = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_MARKDOWN_FRONTMATTER = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
_FRONTMATTER_KEY_VALUE = re.compile(r"^(\w+):\s*(.+)$", re.MULTILINE)
_WORD_BOUNDARY = re.compile(r"\s+")
_BLANK_LINE = re.compile(r"\n{2,}")


def _word_count(text: str) -> int:
    return len(_WORD_BOUNDARY.split(text.strip())) if text.strip() else 0


def _to_tuple(value: object) -> tuple[str, ...]:
    """Normalize a YAML-ish list or comma-delimited string to a tuple."""
    if isinstance(value, list):
        return tuple(str(v).strip() for v in value if str(v).strip())
    if isinstance(value, str):
        items = [s.strip() for s in value.split(",") if s.strip()]
        return tuple(items)
    return ()


@dataclass(slots=True)
class MetadataExtractor:
    """Parses title, author, tags, topics, and summary from document text.

    Format-specific strategies:
        Markdown   — YAML frontmatter + first ``#`` heading.
        JSON       — well-known fields (title/name, author/owner, tags/labels).
        Text/other — heuristic first-line title, first-paragraph summary.

    No AI or LLM calls are made; extraction is deterministic and rule-based.
    """

    max_title_length: int = 120
    max_summary_length: int = 400
    max_topics: int = 20

    async def extract(
        self,
        content: str,
        *,
        content_type: str | None = None,
        hint_tags: tuple[str, ...] = (),
        document_id: str | None = None,
    ) -> ExtractedMetadata:
        """Return ExtractedMetadata for the given text content.

        Raises:
            MetadataExtractionError: If format-specific parsing raises unexpectedly.
        """
        try:
            normalized = (content_type or "").lower().strip().split(";")[0].strip()
            match normalized:
                case "text/markdown" | "text/md":
                    meta = self._extract_markdown(content)
                case "application/json":
                    meta = self._extract_json(content)
                case _:
                    meta = self._extract_text(content)
        except MetadataExtractionError:
            raise
        except Exception as exc:
            raise MetadataExtractionError(
                f"Metadata extraction failed: {exc}",
                document_id=document_id,
                content_type=content_type,
            ) from exc

        # Merge hint tags without duplicating
        merged_tags = tuple(
            dict.fromkeys(list(meta.tags) + [t for t in hint_tags if t not in meta.tags])
        )
        return ExtractedMetadata(
            title=meta.title,
            author=meta.author,
            language=meta.language,
            tags=merged_tags,
            topics=meta.topics,
            entity_refs=meta.entity_refs,
            word_count=meta.word_count or _word_count(content),
            summary=meta.summary,
            custom=meta.custom,
        )

    def _extract_markdown(self, content: str) -> ExtractedMetadata:
        frontmatter: dict[str, object] = {}
        body = content
        fm_match = _MARKDOWN_FRONTMATTER.match(content)
        if fm_match:
            for kv in _FRONTMATTER_KEY_VALUE.finditer(fm_match.group(1)):
                frontmatter[kv.group(1).lower()] = kv.group(2).strip()
            body = content[fm_match.end():].strip()

        title: str | None = None
        raw_title = frontmatter.get("title")
        if isinstance(raw_title, str):
            title = raw_title[: self.max_title_length]
        if title is None:
            h1 = _MARKDOWN_H1.search(body)
            if h1:
                title = h1.group(1).strip()[: self.max_title_length]

        author_raw = frontmatter.get("author") or frontmatter.get("authors")
        author = str(author_raw).strip() if author_raw else None
        tags = _to_tuple(frontmatter.get("tags") or frontmatter.get("labels") or "")
        topics = _to_tuple(frontmatter.get("topics") or frontmatter.get("categories") or "")
        language = str(frontmatter.get("language", "")).strip() or None

        paragraphs = [p.strip() for p in _BLANK_LINE.split(body) if p.strip()]
        summary: str | None = None
        for para in paragraphs:
            stripped = re.sub(r"[#*`_\[\]()]", "", para).strip()
            if stripped and not para.lstrip().startswith("#"):
                summary = stripped[: self.max_summary_length]
                break

        return ExtractedMetadata(
            title=title,
            author=author,
            language=language,
            tags=tags,
            topics=topics[: self.max_topics],
            word_count=_word_count(body),
            summary=summary,
        )

    def _extract_json(self, content: str) -> ExtractedMetadata:
        try:
            obj = json.loads(content)
        except json.JSONDecodeError:
            return self._extract_text(content)

        if not isinstance(obj, dict):
            return ExtractedMetadata()

        title_raw = obj.get("title") or obj.get("name") or obj.get("subject")
        title = str(title_raw)[: self.max_title_length] if title_raw else None

        author_raw = obj.get("author") or obj.get("owner") or obj.get("created_by")
        author = str(author_raw) if author_raw else None

        tags = _to_tuple(obj.get("tags") or obj.get("labels") or obj.get("keywords") or "")
        topics = tuple(
            k for k in obj if isinstance(obj[k], (str, int, float)) and k not in
            {"title", "name", "author", "owner", "tags", "labels", "id", "version"}
        )[: self.max_topics]

        summary_raw = obj.get("description") or obj.get("summary") or obj.get("abstract")
        summary = str(summary_raw)[: self.max_summary_length] if summary_raw else None

        return ExtractedMetadata(
            title=title,
            author=author,
            tags=tags,
            topics=topics,
            word_count=_word_count(content),
            summary=summary,
        )

    def _extract_text(self, content: str) -> ExtractedMetadata:
        lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
        title: str | None = None
        if lines:
            title = lines[0][: self.max_title_length]

        paragraphs = [p.strip() for p in _BLANK_LINE.split(content) if p.strip()]
        summary: str | None = None
        if paragraphs:
            summary = paragraphs[0][: self.max_summary_length]

        return ExtractedMetadata(
            title=title,
            word_count=_word_count(content),
            summary=summary,
        )
