from __future__ import annotations

import importlib.util
from pathlib import Path


_RUNNER = Path(__file__).parents[2] / "searxng" / "run.py"
_SPEC = importlib.util.spec_from_file_location("local_searxng_runner", _RUNNER)
assert _SPEC and _SPEC.loader
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)

_ENGINE = Path(__file__).parents[2] / "searxng" / "engines" / "mwmbl_safe.py"
_ENGINE_SPEC = importlib.util.spec_from_file_location("local_mwmbl_safe", _ENGINE)
assert _ENGINE_SPEC and _ENGINE_SPEC.loader
mwmbl = importlib.util.module_from_spec(_ENGINE_SPEC)
_ENGINE_SPEC.loader.exec_module(mwmbl)


def test_redacts_search_query_parameters_from_logs():
    message = "GET https://search.example/search?q=private+terms&source=web"
    assert runner._redact_search_terms(message) == (
        "GET https://search.example/search?q=[redacted]&source=web"
    )


def test_redacts_common_query_parameter_names_without_changing_other_urls():
    assert runner._redact_search_terms("/api?query=secret&page=1") == "/api?query=[redacted]&page=1"
    assert runner._redact_search_terms("/api?s=secret") == "/api?s=[redacted]"
    assert runner._redact_search_terms("https://example.com/health") == "https://example.com/health"


def test_mwmbl_adapter_handles_missing_extracts():
    class Response:
        @staticmethod
        def json():
            return [
                {
                    "url": "https://example.com/a",
                    "title": [{"value": "Example"}],
                    "extract": [],
                },
                {"url": "https://example.com/b", "title": [], "extract": [{"value": "B"}]},
            ]

    assert mwmbl.response(Response()) == [
        {"url": "https://example.com/a", "title": "Example", "content": ""},
        {"url": "https://example.com/b", "title": "https://example.com/b", "content": "B"},
    ]
