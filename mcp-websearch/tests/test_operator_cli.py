"""Operator CLI configuration propagation tests."""

from __future__ import annotations

import os
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
        "WEBSEARCH_BRAVE_TIMEOUT": "4.5",
        "DECODO_FALLBACK_ENABLED": "true",
        "DECODO_TIMEOUT": "21",
        "DECODO_RESPONSE_MAX_BYTES": "1048576",
        "WEBSEARCH_FETCH_OPERATION_TIMEOUT": "55",
    }
    env.pop("MCP_PORT", None)
    env.pop("LOCAL_SEARCH_INSTALL_CONFIG", None)
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


def test_generated_plist_propagates_search_mode_provider_timeouts_and_port(tmp_path):
    plist = _source_and_run("mcp_plist", tmp_path, {"MCP_PORT": "18889"})
    assert "<key>MCP_PORT</key><string>18889</string>" in plist
    assert "<key>WEBSEARCH_PROVIDER_STACK</key><string>brave</string>" in plist
    assert "<key>WEBSEARCH_SEARCH_MODE</key><string>sensitive</string>" in plist
    assert "<key>WEBSEARCH_TOTAL_TIMEOUT</key><string>17</string>" in plist
    assert "<key>WEBSEARCH_BRAVE_TIMEOUT</key><string>4.5</string>" in plist
    assert "<key>DECODO_FALLBACK_ENABLED</key><string>true</string>" in plist
    assert "<key>DECODO_TIMEOUT</key><string>21</string>" in plist
    assert "<key>DECODO_RESPONSE_MAX_BYTES</key><string>1048576</string>" in plist
    assert "<key>WEBSEARCH_FETCH_OPERATION_TIMEOUT</key><string>55</string>" in plist


def test_env_command_reports_propagated_search_configuration(tmp_path):
    output = _source_and_run("cmd_env", tmp_path)
    assert "MCP_PORT=8889" in output
    assert "WEBSEARCH_PROVIDER_STACK=brave" in output
    assert "WEBSEARCH_SEARCH_MODE=sensitive" in output
    assert "WEBSEARCH_TOTAL_TIMEOUT=17" in output
    assert "WEBSEARCH_BRAVE_TIMEOUT=4.5" in output
    assert "DECODO_FALLBACK_ENABLED=true" in output
    assert "DECODO_TIMEOUT=21" in output
    assert "DECODO_RESPONSE_MAX_BYTES=1048576" in output
    assert "WEBSEARCH_FETCH_OPERATION_TIMEOUT=55" in output


def test_fresh_shell_recovers_persisted_mcp_port(tmp_path):
    install_config = tmp_path / "data" / "install.env"
    install_config.parent.mkdir()
    install_config.write_text("MCP_PORT=18889\n", encoding="utf-8")
    install_config.chmod(0o600)
    output = _source_and_run(
        "cmd_env",
        tmp_path,
        {"LOCAL_SEARCH_INSTALL_CONFIG": str(install_config)},
    )
    assert "MCP_PORT=18889" in output
    assert f"INSTALL_CONFIG={install_config}" in output


def test_fresh_shell_ignores_nonprivate_install_config(tmp_path):
    install_config = tmp_path / "data" / "install.env"
    install_config.parent.mkdir()
    install_config.write_text("MCP_PORT=18889\n", encoding="utf-8")
    install_config.chmod(0o644)
    output = _source_and_run(
        "cmd_env",
        tmp_path,
        {"LOCAL_SEARCH_INSTALL_CONFIG": str(install_config)},
    )
    assert "MCP_PORT=8889" in output


def test_install_config_persists_explicit_mcp_port(tmp_path):
    install_config = tmp_path / "data" / "install.env"
    _source_and_run(
        "persist_install_config",
        tmp_path,
        {
            "LOCAL_SEARCH_INSTALL_CONFIG": str(install_config),
            "MCP_PORT": "28889",
        },
    )
    assert "MCP_PORT=28889" in install_config.read_text(encoding="utf-8").splitlines()
    assert install_config.stat().st_mode & 0o777 == 0o600


def test_decodo_preflight_rejects_unsafe_optional_key(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    brave_key = data_dir / "brave_key"
    brave_key.write_text("brave-token\n", encoding="utf-8")
    brave_key.chmod(0o600)
    key_file = data_dir / "decodo_key"
    key_file.write_text("secret-token\n", encoding="utf-8")
    key_file.chmod(0o644)
    with pytest.raises(subprocess.CalledProcessError) as error:
        _source_and_run("check_provider_credentials", tmp_path)
    assert "Decodo key must be a regular, owner-only" in error.value.stderr


def test_stdio_exports_all_decodo_runtime_settings():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "export DECODO_FALLBACK_ENABLED DECODO_TIMEOUT DECODO_RESPONSE_MAX_BYTES" in script
    assert "export WEBSEARCH_FETCH_OPERATION_TIMEOUT" in script


def test_operator_rejects_invalid_mcp_port(tmp_path):
    with pytest.raises(subprocess.CalledProcessError) as error:
        _source_and_run("validate_search_configuration", tmp_path, {"MCP_PORT": "70000"})
    assert "MCP_PORT must be between 1 and 65535" in error.value.stderr


@pytest.mark.parametrize("port", [" 18889", "+18889", "١٨٨٨٩"])
def test_operator_rejects_noncanonical_mcp_port(tmp_path, port):
    with pytest.raises(subprocess.CalledProcessError) as error:
        _source_and_run("validate_search_configuration", tmp_path, {"MCP_PORT": port})
    assert "MCP_PORT must contain only ASCII digits" in error.value.stderr


def test_invalid_port_does_not_poison_existing_install_config(tmp_path):
    install_config = tmp_path / "data" / "install.env"
    install_config.parent.mkdir()
    install_config.write_text("MCP_PORT=18889\n", encoding="utf-8")
    with pytest.raises(subprocess.CalledProcessError):
        _source_and_run(
            "persist_install_config",
            tmp_path,
            {
                "LOCAL_SEARCH_INSTALL_CONFIG": str(install_config),
                "MCP_PORT": "70000",
            },
        )
    assert install_config.read_text(encoding="utf-8") == "MCP_PORT=18889\n"


def test_persistence_refuses_symlinked_install_config(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("unchanged\n", encoding="utf-8")
    install_config = data_dir / "install.env"
    install_config.symlink_to(victim)
    with pytest.raises(subprocess.CalledProcessError) as error:
        _source_and_run(
            "persist_install_config",
            tmp_path,
            {
                "LOCAL_SEARCH_INSTALL_CONFIG": str(install_config),
                "MCP_PORT": "18889",
            },
        )
    assert "refusing unsafe install config" in error.value.stderr
    assert victim.read_text(encoding="utf-8") == "unchanged\n"


def test_operator_rejects_noninteger_decodo_response_limit(tmp_path):
    with pytest.raises(subprocess.CalledProcessError) as error:
        _source_and_run(
            "validate_search_configuration",
            tmp_path,
            {"DECODO_RESPONSE_MAX_BYTES": "1.5"},
        )
    assert "must be an integer" in error.value.stderr


def test_update_reexecs_new_script_and_retires_legacy_only_after_verify():
    script = SCRIPT.read_text(encoding="utf-8")
    assert 'exec "$ROOT_DIR/scripts/local-search" _update-after-pull' in script
    install_body = script.split("cmd_install() {", 1)[1].split("cmd_uninstall() {", 1)[0]
    assert install_body.index("validate_search_configuration") < install_body.index("ensure_mcp_venv")
    assert install_body.index("verify || die") < install_body.index("retire_legacy_searxng")
    update_body = script.split("cmd_update_after_pull() {", 1)[1].split("cmd_update() {", 1)[0]
    assert update_body.index("check_provider_credentials") < update_body.index("install_plist")
    assert update_body.index("verify || die") < update_body.index("retire_legacy_searxng")
    bootstrap_body = script.split("bootstrap_service() {", 1)[1].split("_wait_port_free() {", 1)[0]
    assert bootstrap_body.index("launchctl enable") < bootstrap_body.index("launchctl bootstrap")
