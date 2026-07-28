from __future__ import annotations

import os
import plistlib
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[2]
TUNNEL = ROOT / "scripts" / "local-search-tunnel"
SERVER_CLI = ROOT / "scripts" / "local-search"


def _private_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _fake_client_env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl = bin_dir / "launchctl"
    launchctl.write_text("#!/bin/sh\n[ \"${1:-}\" = print ] && exit 1\nexit 0\n")
    launchctl.chmod(0o755)
    lsof = bin_dir / "lsof"
    lsof.write_text("#!/bin/sh\nexit 1\n")
    lsof.chmod(0o755)
    return {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }


def test_tunnel_render_has_exact_restricted_foreground_ssh_arguments():
    result = subprocess.run(
        [str(TUNNEL), "render", "--ssh-host", "local-search-server"],
        check=True,
        capture_output=True,
    )
    plist = plistlib.loads(result.stdout)
    assert plist["Label"] == "com.local.local-search-tunnel"
    assert plist["ProgramArguments"] == [
        "/usr/bin/ssh",
        "-N",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ForwardAgent=no",
        "-L",
        "127.0.0.1:8889:127.0.0.1:8889",
        "--",
        "local-search-server",
    ]
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert plist["ThrottleInterval"] == 30
    assert plist["Umask"] == 0o77
    assert "EnvironmentVariables" not in plist


def test_tunnel_rejects_option_or_xml_injection_destinations():
    for value in ("-oProxyCommand=bad", "host<bad", "host bad"):
        result = subprocess.run(
            [str(TUNNEL), "render", "--ssh-host", value],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0


def test_tunnel_install_is_idempotent_and_private(tmp_path):
    env = _fake_client_env(tmp_path)
    command = [
        str(TUNNEL),
        "install",
        "--ssh-host",
        "local-search-server",
        "--no-start",
    ]
    subprocess.run(command, check=True, env=env, capture_output=True)
    home = Path(env["HOME"])
    plist_path = home / "Library/LaunchAgents/com.local.local-search-tunnel.plist"
    log_dir = home / "Library/Logs/local-search-tunnel"
    log_path = log_dir / "ssh-tunnel.log"
    first = plist_path.read_bytes()

    subprocess.run(command, check=True, env=env, capture_output=True)
    assert plist_path.read_bytes() == first
    assert _private_mode(plist_path) == 0o600
    assert _private_mode(log_dir) == 0o700
    assert _private_mode(log_path) == 0o600


def test_tunnel_log_rotation_preserves_private_archive(tmp_path):
    env = _fake_client_env(tmp_path)
    subprocess.run(
        [str(TUNNEL), "install", "--ssh-host", "local-search-server", "--no-start"],
        check=True,
        env=env,
        capture_output=True,
    )
    home = Path(env["HOME"])
    log_path = home / "Library/Logs/local-search-tunnel/ssh-tunnel.log"
    log_path.write_text("historical query-bearing diagnostic\n")
    subprocess.run(
        [str(TUNNEL), "rotate-logs", "--yes"],
        check=True,
        env=env,
        capture_output=True,
    )
    archives = list((log_path.parent / "archive").glob("ssh-tunnel.*.log"))
    assert len(archives) == 1
    assert archives[0].read_text() == "historical query-bearing diagnostic\n"
    assert _private_mode(archives[0]) == 0o600
    assert _private_mode(archives[0].parent) == 0o700
    assert log_path.read_text() == ""
    assert _private_mode(log_path) == 0o600


def test_tunnel_uninstall_preserves_logs(tmp_path):
    env = _fake_client_env(tmp_path)
    subprocess.run(
        [str(TUNNEL), "install", "--ssh-host", "local-search-server", "--no-start"],
        check=True,
        env=env,
        capture_output=True,
    )
    home = Path(env["HOME"])
    log_path = home / "Library/Logs/local-search-tunnel/ssh-tunnel.log"
    log_path.write_text("diagnostic\n")
    subprocess.run([str(TUNNEL), "uninstall"], check=True, env=env, capture_output=True)
    assert log_path.read_text() == "diagnostic\n"
    assert not (home / "Library/LaunchAgents/com.local.local-search-tunnel.plist").exists()


def test_server_launchagent_plists_set_private_umask(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path / "home")}
    for function in ("mcp_plist",):
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; "$2"', "test", str(SERVER_CLI), function],
            check=True,
            env=env,
            capture_output=True,
        )
        plist = plistlib.loads(result.stdout)
        assert plist["Umask"] == 0o77
        assert plist["RunAtLoad"] is True
        assert plist["KeepAlive"] is True


def test_server_plist_writer_uses_private_mode(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path / "home")}
    subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; write_plist test.private "$(mcp_plist)"',
            "test",
            str(SERVER_CLI),
        ],
        check=True,
        env=env,
        capture_output=True,
    )
    path = Path(env["HOME"]) / "Library/LaunchAgents/test.private.plist"
    assert _private_mode(path) == 0o600
    assert plistlib.loads(path.read_bytes())["Umask"] == 0o77
