# Remote MCP client over a persistent SSH tunnel

This is the SSH remote-access path for local-search. For URL-only access, see
[opt-in Tailscale HTTPS mode](remote-mcp-over-tailnet.md). SSH keeps the broker
bound to server loopback and exposes only a client-local endpoint:

```text
MCP client -> http://127.0.0.1:8889/mcp
           -> SSH -L tunnel
           -> server 127.0.0.1:8889
```

The helper is generic: it does not install Pi, edit `~/.pi`, store a private key,
or commit a username, address, token, or secret. It intentionally supports the
standard server port `8889` only so every client has the documented endpoint;
keep `MCP_PORT=8889` on a server used by this helper.

## 1. Create a dedicated client key

On the client Mac:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_local_search -C local-search-tunnel
chmod 600 ~/.ssh/id_ed25519_local_search
```

Enroll the server host key interactively before enabling the LaunchAgent. Check
the fingerprint through a separate trusted channel; do not disable host-key
checking.

Add a client-local SSH config entry (replace every example value):

```sshconfig
Host local-search-server
    HostName <server-address-or-tailnet-name>
    User <restricted-server-user>
    IdentityFile ~/.ssh/id_ed25519_local_search
    IdentitiesOnly yes
    AddKeysToAgent yes
    UseKeychain yes
```

A Tailscale address/name is suitable as the SSH transport. The MCP service still
must not bind to the tailnet interface.

## 2. Restrict the public key on the server

The minimum per-key restriction is an `authorized_keys` entry like this (one
physical line, followed by the client's actual public key):

```text
restrict,port-forwarding,permitopen="127.0.0.1:8889",command="/usr/bin/false" ssh-ed25519 AAAA... local-search-tunnel
```

Why each option exists:

- `restrict` disables PTY, agent/X11 forwarding, user rc, and forwarding by
  default.
- `port-forwarding` re-enables forwarding for this key.
- `permitopen="127.0.0.1:8889"` limits local (`ssh -L`) forwarding to this broker.
- `command="/usr/bin/false"` rejects shell/command sessions. It does not run for
  the helper's `ssh -N`, which requests no session.

`permitopen` does not prohibit every reverse-forwarding form. For the strongest
isolation, use a dedicated non-admin server account and add a reviewed `sshd_config`
`Match User` block:

```text
Match User <restricted-server-user>
    AllowTcpForwarding local
    PermitOpen 127.0.0.1:8889
    X11Forwarding no
    AllowAgentForwarding no
    PermitTTY no
```

Validate SSH configuration before reload (`sudo sshd -t`). Server account and
SSH daemon changes are deliberately not automated by this repository.

## 3. Test SSH non-interactively

From the client, confirm the dedicated alias authenticates without a prompt:

```bash
ssh -N -T \
  -o BatchMode=yes \
  -o ExitOnForwardFailure=yes \
  -o ConnectTimeout=10 \
  -L 127.0.0.1:8889:127.0.0.1:8889 \
  local-search-server
```

In another terminal:

```bash
curl -fsS http://127.0.0.1:8889/live
```

Stop the temporary foreground SSH process before installing the LaunchAgent.

## 4. Install the persistent tunnel

From a checkout or copy of this repository on the client:

```bash
chmod +x scripts/local-search-tunnel
scripts/local-search-tunnel render --ssh-host local-search-server \
  | plutil -lint -
scripts/local-search-tunnel install --ssh-host local-search-server
```

The generated LaunchAgent uses:

- foreground `/usr/bin/ssh -N -T`;
- `BatchMode=yes` and `ExitOnForwardFailure=yes`;
- 30-second server keepalives with a three-failure limit;
- a 10-second connect timeout and one SSH connection attempt per process;
- launchd `RunAtLoad`, `KeepAlive`, and a 30-second restart throttle;
- client and server loopback endpoints fixed to port `8889`;
- `Umask 077`, a mode-`0600` plist/log, and a mode-`0700` log directory.

Install is idempotent. `--no-start` writes and validates the plist without
loading it:

```bash
scripts/local-search-tunnel install --ssh-host local-search-server --no-start
scripts/local-search-tunnel start
```

## 5. Status and verification

```bash
scripts/local-search-tunnel status
scripts/local-search-tunnel verify
curl -fsS http://127.0.0.1:8889/live
curl -fsS http://127.0.0.1:8889/ready
curl -fsS http://127.0.0.1:8889/health | python3 -m json.tool
curl -fsS -X POST http://127.0.0.1:8889/mcp \
  -H 'content-type: application/json' \
  -H 'accept: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Configure the MCP client to use exactly:

```text
http://127.0.0.1:8889/mcp
```

Keep client-specific policy and configuration in that client's owning project.
For Pi, that means `pi-shared`, not this repository.

## Operations

```bash
scripts/local-search-tunnel stop
scripts/local-search-tunnel start
scripts/local-search-tunnel rotate-logs --yes
scripts/local-search-tunnel uninstall
```

Log rotation stops the tunnel briefly, moves a non-empty log into the private
`archive` directory, creates a fresh private log, and restarts only if it was
loaded. It never deletes the archived log. Uninstall removes the LaunchAgent but
retains logs for explicit operator review/removal.

If the tunnel repeatedly exits, inspect:

```bash
tail -100 ~/Library/Logs/local-search-tunnel/ssh-tunnel.log
ssh -vv -N -T -o BatchMode=yes \
  -L 127.0.0.1:8889:127.0.0.1:8889 local-search-server
```

Common causes are an unenrolled host key, a key unavailable to non-interactive
SSH, a changed SSH alias, server sleep/offline state, or another client process
already listening on `127.0.0.1:8889`.
