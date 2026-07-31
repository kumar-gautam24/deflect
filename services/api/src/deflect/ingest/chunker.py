"""Splits markdown into retrievable chunks that carry their heading context."""

import re
from dataclasses import dataclass

HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
FENCE = re.compile(r"^\s*```")


@dataclass(frozen=True)
class TextChunk:
    heading_path: str
    text: str
    position: int


def _split_sections(source: str) -> list[tuple[str, str]]:
    sections: list[tuple[list[str], list[str]]] = []
    stack: list[str] = []
    body: list[str] = []
    in_fence = False

    def flush() -> None:
        if body:
            sections.append((list(stack), list(body)))
        body.clear()

    for line in source.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence

        match = None if in_fence else HEADING.match(line)
        if match is None:
            body.append(line)
            continue

        flush()
        depth = len(match.group(1))
        del stack[depth - 1 :]
        stack.append(match.group(2).strip())

    flush()
    return [(" > ".join(path), "\n".join(lines).strip()) for path, lines in sections]


def _split_oversized(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    parts: list[str] = []
    start = 0
    while start < len(text):
        parts.append(text[start : start + max_chars])
        start += max_chars - overlap_chars
    return parts


def chunk_markdown(
    source: str, max_chars: int = 1200, overlap_chars: int = 150
) -> list[TextChunk]:
    """Chunk on heading boundaries, splitting only sections that exceed max_chars.

    Heading boundaries are used rather than a fixed window because a documentation
    section is the unit an answer cites, and the heading path is what makes a
    citation readable.
    """
    if overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be smaller than max_chars")

    chunks: list[TextChunk] = []
    for heading_path, body in _split_sections(source):
        if not body:
            continue
        for part in _split_oversized(body, max_chars, overlap_chars):
            chunks.append(TextChunk(heading_path, part, len(chunks)))
    return chunks
