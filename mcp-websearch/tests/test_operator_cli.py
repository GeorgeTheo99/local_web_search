"""Operator CLI configuration propagation tests."""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "local-search"


def _source_and_run(
    command: str,
    tmp_path: Path,
    env_overrides: dict[str, str] | None = None,
) -> str:
    env = {
        **os.environ,
        "LOCAL_SEARCH_DATA_DIR": str(tmp_path / "data"),
        "WEBSEARCH_PROVIDER_STACK": "brave",
        "WEBSEARCH_SEARCH_MODE": "sensitive",
        "WEBSEARCH_TOTAL_TIMEOUT": "17",
        "WEBSEARCH_SEARXNG_TIMEOUT": "6",
        "WEBSEARCH_BRAVE_TIMEOUT": "4.5",
        "WEBSEARCH_QUALITY_GATE": "shadow",
        "WEBSEARCH_QUALITY_MIN_RESULTS": "4",
        "WEBSEARCH_QUALITY_MIN_DOMAINS": "3",
        "WEBSEARCH_QUALITY_DUPLICATE_FRACTION": "0.7",
    }
    env.update(env_overrides or {})
    completed = subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}"; {command}'],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return completed.stdout


def test_ensure_data_dir_creates_private_cache_directory(tmp_path):
    _source_and_run("ensure_data_dir", tmp_path)
    data_dir = tmp_path / "data"
    cache_dir = data_dir / "cache"
    assert data_dir.stat().st_mode & 0o777 == 0o700
    assert cache_dir.stat().st_mode & 0o777 == 0o700


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


def test_dual_stack_is_valid_and_requires_brave_credential(tmp_path):
    overrides = {"WEBSEARCH_PROVIDER_STACK": "searxng+brave"}
    assert _source_and_run(
        "validate_search_configuration; printf passed", tmp_path, overrides
    ) == "passed"
    with pytest.raises(subprocess.CalledProcessError) as error:
        _source_and_run("check_provider_credentials", tmp_path, overrides)
    assert "Brave stack requires an owner-only nonempty key" in error.value.stderr

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    key_file = data_dir / "brave_key"
    key_file.write_text("test-key\n", encoding="utf-8")
    key_file.chmod(0o600)
    assert _source_and_run(
        "check_provider_credentials; printf passed", tmp_path, overrides
    ) == "passed"


def test_searxng_plist_generates_and_injects_private_secret(tmp_path):
    plist = plistlib.loads(_source_and_run("searxng_plist", tmp_path).encode())
    secret_file = tmp_path / "data" / "searxng_secret_key"
    secret = secret_file.read_text(encoding="utf-8").strip()
    assert len(secret) == 64
    assert all(character in "0123456789abcdef" for character in secret)
    assert secret_file.stat().st_mode & 0o777 == 0o600
    assert plist["EnvironmentVariables"]["SEARXNG_SECRET"] == secret


def test_generated_plist_propagates_search_mode_and_provider_timeouts(tmp_path):
    plist = _source_and_run("mcp_plist", tmp_path)
    assert "<key>WEBSEARCH_PROVIDER_STACK</key><string>brave</string>" in plist
    assert "<key>WEBSEARCH_SEARCH_MODE</key><string>sensitive</string>" in plist
    assert "<key>WEBSEARCH_TOTAL_TIMEOUT</key><string>17</string>" in plist
    assert "<key>WEBSEARCH_SEARXNG_TIMEOUT</key><string>6</string>" in plist
    assert "<key>WEBSEARCH_BRAVE_TIMEOUT</key><string>4.5</string>" in plist
    assert "<key>WEBSEARCH_QUALITY_GATE</key><string>shadow</string>" in plist
    assert "<key>WEBSEARCH_QUALITY_MIN_RESULTS</key><string>4</string>" in plist
    assert "<key>WEBSEARCH_QUALITY_MIN_DOMAINS</key><string>3</string>" in plist
    assert (
        "<key>WEBSEARCH_QUALITY_DUPLICATE_FRACTION</key><string>0.7</string>"
        in plist
    )


def test_env_command_reports_propagated_search_configuration(tmp_path):
    output = _source_and_run("cmd_env", tmp_path)
    assert "WEBSEARCH_PROVIDER_STACK=brave" in output
    assert "WEBSEARCH_SEARCH_MODE=sensitive" in output
    assert "WEBSEARCH_TOTAL_TIMEOUT=17" in output
    assert "WEBSEARCH_SEARXNG_TIMEOUT=6" in output
    assert "WEBSEARCH_BRAVE_TIMEOUT=4.5" in output
    assert "WEBSEARCH_QUALITY_GATE=shadow" in output
    assert "WEBSEARCH_QUALITY_MIN_RESULTS=4" in output
    assert "WEBSEARCH_QUALITY_MIN_DOMAINS=3" in output
    assert "WEBSEARCH_QUALITY_DUPLICATE_FRACTION=0.7" in output
