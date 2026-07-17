#!/usr/bin/env python3
"""Offline, bounded HTML main-content extraction for local-search.

The parent broker performs all networking and SSRF validation. This module only
accepts already-downloaded UTF-8 HTML on stdin and emits bounded readable text.
"""

from __future__ import annotations

import argparse
import os
import re
import resource
import sys
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

from readability import Document

MAIN_CONTENT_MAX_CHARS = 5_000_000
MAIN_CONTENT_MIN_CHARS = 200
MAX_ATTACHMENT_LINKS = 20
MAX_ATTACHMENT_LABEL_CHARS = 120
MAX_ATTACHMENT_URL_CHARS = 2048
ATTACHMENT_SUFFIXES = {
    ".csv",
    ".doc",
    ".docx",
    ".pdf",
    ".ppt",
    ".pptx",
    ".rtf",
    ".txt",
    ".xls",
    ".xlsx",
    ".zip",
}
TRUNCATION_SUFFIX = "\n\n... (truncated)"


class TextExtractor(HTMLParser):
    """Extract readable text while preserving safe HTTP(S) links."""

    def __init__(self, base_url: str):
        super().__init__()
        self._base_url = base_url
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._anchor_href: str | None = None
        self._anchor_chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        href = next((value for key, value in attrs if key.lower() == "href"), None)
        if tag == "base" and href:
            candidate = urllib.parse.urljoin(self._base_url, href)
            if urllib.parse.urlparse(candidate).scheme in {"http", "https"}:
                self._base_url = candidate
            return
        if tag != "a" or self._anchor_href is not None or not href:
            return
        candidate = urllib.parse.urljoin(self._base_url, href)
        if urllib.parse.urlparse(candidate).scheme in {"http", "https"}:
            self._anchor_href = candidate
            self._anchor_chunks = []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "a" and self._anchor_href is not None:
            label = " ".join(self._anchor_chunks).strip() or self._anchor_href
            self._chunks.append(f"[{label}]({self._anchor_href})")
            self._anchor_href = None
            self._anchor_chunks = []

    def handle_data(self, data):
        if self._skip_depth:
            return
        text = " ".join(data.split())
        if not text:
            return
        if self._anchor_href is not None:
            self._anchor_chunks.append(text)
        else:
            self._chunks.append(text)

    def get_text(self) -> str:
        return "\n".join(self._chunks)


class AttachmentLinkExtractor(HTMLParser):
    """Collect bounded document/archive links that Readability may omit."""

    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.links: list[tuple[str, str]] = []
        self._seen: set[str] = set()
        self._skip_depth = 0
        self._anchor_url: str | None = None
        self._anchor_is_attachment = False
        self._anchor_chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        attributes = {key.lower(): value for key, value in attrs}
        href = attributes.get("href")
        if tag == "base" and href:
            candidate = urllib.parse.urljoin(self.base_url, href)
            if urllib.parse.urlparse(candidate).scheme in {"http", "https"}:
                self.base_url = candidate
            return
        if tag != "a" or self._anchor_url is not None or not href:
            return
        candidate = urllib.parse.urljoin(self.base_url, href)
        parsed = urllib.parse.urlparse(candidate)
        if parsed.scheme not in {"http", "https"}:
            return
        if len(candidate) > MAX_ATTACHMENT_URL_CHARS:
            return
        suffix = Path(urllib.parse.unquote(parsed.path)).suffix.lower()
        self._anchor_url = candidate
        self._anchor_is_attachment = "download" in attributes or suffix in ATTACHMENT_SUFFIXES
        self._anchor_chunks = []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag != "a" or self._anchor_url is None:
            return
        if (
            self._anchor_is_attachment
            and self._anchor_url not in self._seen
            and len(self.links) < MAX_ATTACHMENT_LINKS
        ):
            label = " ".join(self._anchor_chunks).strip() or Path(
                urllib.parse.unquote(urllib.parse.urlparse(self._anchor_url).path)
            ).name or self._anchor_url
            label = truncate_text(label, MAX_ATTACHMENT_LABEL_CHARS, "...")
            self._seen.add(self._anchor_url)
            self.links.append((label, self._anchor_url))
        self._anchor_url = None
        self._anchor_is_attachment = False
        self._anchor_chunks = []

    def handle_data(self, data):
        if self._skip_depth or self._anchor_url is None:
            return
        text = " ".join(data.split())
        if text:
            self._anchor_chunks.append(text)


def extract_with_text_parser(html: str, base_url: str) -> str:
    parser = TextExtractor(base_url)
    parser.feed(html)
    parser.close()
    return parser.get_text()


def truncate_text(text: str, max_chars: int, suffix: str = TRUNCATION_SUFFIX) -> str:
    if len(text) <= max_chars:
        return text
    if len(suffix) >= max_chars:
        return suffix[:max_chars]
    return text[: max_chars - len(suffix)] + suffix


def _attachment_section(
    attachments: list[tuple[str, str]],
    max_chars: int,
) -> str:
    lines = ["Attachments:"]
    used = len(lines[0])
    for label, url in attachments:
        line = f"- [{label}]({url})"
        required = 1 + len(line)
        if used + required > max_chars:
            break
        lines.append(line)
        used += required
    return "\n".join(lines) if len(lines) > 1 else ""


def _assemble_output(
    article: str,
    attachments: list[tuple[str, str]],
    max_chars: int,
) -> str:
    bounded_article = truncate_text(article, max_chars)
    missing = [(label, url) for label, url in attachments if url not in bounded_article]
    if not missing:
        return bounded_article

    # Shrinking the article can remove more in-article attachment links. Iterate
    # until the reserved section and article prefix agree, bounded by link count.
    for _ in range(len(attachments) + 1):
        attachment_text = _attachment_section(missing, max_chars)
        if not attachment_text:
            return bounded_article
        article_budget = max(0, max_chars - len(attachment_text) - 2)
        bounded_article = truncate_text(article, article_budget).rstrip()
        expanded_missing = [
            (label, url) for label, url in attachments if url not in bounded_article
        ]
        if expanded_missing == missing:
            break
        missing = expanded_missing

    attachment_text = _attachment_section(missing, max_chars)
    if not bounded_article:
        return attachment_text
    return f"{bounded_article}\n\n{attachment_text}"


def extract_html_content(html: str, base_url: str, max_chars: int = 20_000) -> str:
    """Extract main content without networking and preserve bounded attachments."""
    max_chars = min(50_000, max(1, int(max_chars)))
    attachment_parser = AttachmentLinkExtractor(base_url)
    try:
        attachment_parser.feed(html)
        attachment_parser.close()
    except Exception:
        attachment_parser = AttachmentLinkExtractor(base_url)

    effective_base = attachment_parser.base_url
    article = ""
    if len(html) <= MAIN_CONTENT_MAX_CHARS:
        try:
            # Pass the original page URL so a relative <base> is resolved exactly once.
            summary = Document(html, url=base_url).summary(html_partial=True)
            candidate = extract_with_text_parser(summary, effective_base).strip()
            if len(re.sub(r"\s+", "", candidate)) >= MAIN_CONTENT_MIN_CHARS:
                article = candidate
        except Exception:
            article = ""
    if not article:
        article = extract_with_text_parser(html, effective_base).strip()

    return _assemble_output(article, attachment_parser.links, max_chars)


def _apply_resource_limits() -> None:
    """Constrain hostile HTML parsing in the dedicated child process."""
    cpu_seconds = 6
    memory_bytes = 512 * 1024 * 1024
    output_bytes = 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    except (OSError, ValueError):
        pass
    for limit_name in ("RLIMIT_AS", "RLIMIT_DATA"):
        limit = getattr(resource, limit_name, None)
        if limit is None:
            continue
        try:
            resource.setrlimit(limit, (memory_bytes, memory_bytes))
        except (OSError, ValueError):
            pass
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (output_bytes, output_bytes))
    except (OSError, ValueError):
        pass
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    except (OSError, ValueError):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--max-chars", required=True, type=int)
    parser.add_argument("--max-input-bytes", required=True, type=int)
    args = parser.parse_args()
    _apply_resource_limits()
    try:
        html = sys.stdin.buffer.read(args.max_input_bytes + 1)
        if len(html) > args.max_input_bytes:
            raise ValueError("HTML input exceeds child-process limit")
        text = extract_html_content(
            html.decode("utf-8", errors="replace"),
            args.base_url,
            args.max_chars,
        )
        sys.stdout.buffer.write(text.encode("utf-8"))
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: HTML extraction failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
