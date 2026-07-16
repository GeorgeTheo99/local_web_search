#!/usr/bin/env python3
"""Run SearXNG while redacting search terms from operational log messages."""

from __future__ import annotations

import logging
import re
import runpy
from pathlib import Path

# Search engines use many parameter names, so redact every URL query value.
# Some providers (notably Wikipedia REST) place the search term in the path.
_QUERY_VALUE = re.compile(r"([?&][^=&\s#\"']+=)[^&\s#\"']*", re.IGNORECASE)
_SUMMARY_PATH = re.compile(r"(/page/summary/)[^?\s#\"']+", re.IGNORECASE)


def _redact_search_terms(message: str) -> str:
    message = _SUMMARY_PATH.sub(r"\1[redacted]", message)
    return _QUERY_VALUE.sub(r"\1[redacted]", message)


def _install_query_redaction() -> None:
    original_factory = logging.getLogRecordFactory()

    def redacting_factory(*args, **kwargs):
        record = original_factory(*args, **kwargs)
        message = record.getMessage()
        redacted = _redact_search_terms(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return record

    logging.setLogRecordFactory(redacting_factory)


def _install_local_engine_loader() -> None:
    """Load named local engines without modifying the pinned SearXNG checkout."""
    import searx.engines

    upstream_loader = searx.engines.load_engine
    local_engine_dir = str(Path(__file__).with_name("engines"))

    def load_engine(engine_data):
        if engine_data.get("engine") != "mwmbl_safe":
            return upstream_loader(engine_data)
        upstream_dir = searx.engines.ENGINE_DIR
        try:
            searx.engines.ENGINE_DIR = local_engine_dir
            return upstream_loader(engine_data)
        finally:
            searx.engines.ENGINE_DIR = upstream_dir

    searx.engines.load_engine = load_engine


def main() -> None:
    _install_query_redaction()
    _install_local_engine_loader()
    runpy.run_module("searx.webapp", run_name="__main__")


if __name__ == "__main__":
    main()
