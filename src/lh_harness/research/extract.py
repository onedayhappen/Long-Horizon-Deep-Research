from __future__ import annotations

import hashlib
import io

from bs4 import BeautifulSoup
from pypdf import PdfReader

from .models import ParsedBlock, ParsedDocument, TextSpan


def parse_document(raw: bytes, mime: str, *, max_chars: int = 300000, max_pdf_pages: int = 200) -> tuple[ParsedDocument, str]:
    blocks: list[tuple[str, int | None]] = []
    if mime == "text/html":
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        for tag in soup.find_all(["h1", "h2", "h3", "p", "li", "tr"]):
            value = tag.get_text(" ", strip=True)
            if value:
                blocks.append((value, None))
        if not blocks:
            value = soup.get_text(" ", strip=True)
            if value:
                blocks.append((value, None))
        parser_id = "beautifulsoup-html"
    elif mime == "application/pdf":
        reader = PdfReader(io.BytesIO(raw), strict=True)
        if len(reader.pages) > max_pdf_pages:
            raise ValueError("PDF page limit exceeded")
        for number, page in enumerate(reader.pages, 1):
            value = page.extract_text() or ""
            if value:
                blocks.append((value, number))
        parser_id = "pypdf-text"
    else:
        raise ValueError("unsupported MIME type")
    text = "\n".join(value for value, _ in blocks)
    if len(text) > max_chars:
        raise ValueError("parsed text limit exceeded")
    parsed_blocks = []
    cursor = 0
    for index, (value, page) in enumerate(blocks):
        parsed_blocks.append(ParsedBlock(id=f"b{index+1}", start=cursor, end=cursor + len(value), page=page))
        cursor += len(value) + 1
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return ParsedDocument(parser_id=parser_id, parser_version="1", text_blob_hash=sha, language=None, blocks=parsed_blocks), text


def locate(text: str, excerpt: str, *, page: int | None = None, block_id: str | None = None) -> TextSpan:
    start = text.find(excerpt)
    if start < 0 or not excerpt:
        raise ValueError("excerpt absent from parsed text")
    end = start + len(excerpt)
    return TextSpan(text_blob_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(), start=start, end=end, quote_hash=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(), prefix=text[max(0, start-40):start], suffix=text[end:end+40], page=page, block_id=block_id)


def verify_locator(text: str, locator: TextSpan, excerpt: str) -> bool:
    return hashlib.sha256(text.encode("utf-8")).hexdigest() == locator.text_blob_hash and text[locator.start:locator.end] == excerpt and hashlib.sha256(excerpt.encode("utf-8")).hexdigest() == locator.quote_hash
