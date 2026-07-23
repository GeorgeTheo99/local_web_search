"""Operator CLI configuration propagation tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "local-search"


def _source_and_run(command: str, tmp_path: Path) -> str:
    env = {
        **os.environ,
        "LOCAL_SEARCH_DATA_DIR": str(tmp_path / "data"),
        "WEBSEARCH_PROVIDER_STACK": "brave",
        "WEBSEARCH_SEARCH_MODE": "sensitive",
        "WEBSEARCH_TOTAL_TIMEOUT": "17",
        "WEBSEARCH_SEARXNG_TIMEOUT": "6",
        "WEBSEARCH_BRAVE_TIMEOUT": "4.5",
        "WEBSEARCH_SUPPLEMENT_MIN_RESULTS": "4",
    }
    completed = subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}"; {command}'],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return completed.stdout


def test_brave_credential_preflight_fails_with_actionable_message(tmp_path):
    with pytest.raises(subprocess.CalledProcessError) as error:
        _source_and_run("check_provider_credentials", tmp_path)
    assert "Brave stack requires an owner-only nonempty key" in error.value.stderr


def test_brave_credential_preflight_accepts_owner_only_key(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    key_file = data_dir / "brave_key"
    key_file.write_text("test-key\n", encoding="utf-8")
    key_file.chmod(0o600)
    assert _source_and_run("check_provider_credentials; printf passed", tmp_path) == "passed"


def test_generated_plist_propagates_search_mode_and_provider_timeouts(tmp_path):
    plist = _source_and_run("mcp_plist", tmp_path)
    assert "<key>WEBSEARCH_PROVIDER_STACK</key><string>brave</string>" in plist
    assert "<key>WEBSEARCH_SEARCH_MODE</key><string>sensitive</string>" in plist
    assert "<key>WEBSEARCH_TOTAL_TIMEOUT</key><string>17</string>" in plist
    assert "<key>WEBSEARCH_SEARXNG_TIMEOUT</key><string>6</string>" in plist
    assert "<key>WEBSEARCH_BRAVE_TIMEOUT</key><string>4.5</string>" in plist


def test_env_command_reports_propagated_search_configuration(tmp_path):
    output = _source_and_run("cmd_env", tmp_path)
    assert "WEBSEARCH_PROVIDER_STACK=brave" in output
    assert "WEBSEARCH_SEARCH_MODE=sensitive" in output
    assert "WEBSEARCH_TOTAL_TIMEOUT=17" in output
    assert "WEBSEARCH_SEARXNG_TIMEOUT=6" in output
    assert "WEBSEARCH_BRAVE_TIMEOUT=4.5" in output
