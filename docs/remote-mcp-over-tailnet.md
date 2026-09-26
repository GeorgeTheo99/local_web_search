# URL-only MCP access over Tailscale

Opt-in alternative to [SSH forwarding](remote-mcp-over-ssh.md). Every device
permitted to reach this server by Tailscale access rules is trusted to call all
broker tools and consume the server's Brave/Decodo quota. No bearer token or
per-user allowlist is required. Tagged devices work too. Device sharing can also
grant access; review your Tailscale sharing and access rules accordingly.

## Boundary

- The normal broker remains `127.0.0.1:8889`, with its existing local Host/Origin
  checks and diagnostics.
- A **separate** opt-in process listens on `127.0.0.1:8891` (configurable). It
  accepts only `POST /mcp`, the exact configured MagicDNS hostname (optional
  `:443`), and absent Origin or that hostname's HTTPS Origin.
- Tailscale Serve terminates HTTPS and proxies to that dedicated ingress. The
  ingress denies diagnostics, even if a caller supplies `Host: localhost`.
  Forwarded headers and user identity headers do not grant access.
- Local processes are trusted. Host checks are DNS-rebinding/browser defenses,
  **not authentication**. Network authorization is Tailscale's responsibility.
- Never route a public proxy, LAN listener, or **Tailscale Funnel** to either
  listener. Enabling Funnel on the same HTTPS port makes Serve routes public;
  the application cannot distinguish that reliably for tagged clients. Keep
  Funnel off on port 443 and recheck after Tailscale configuration changes.
- This exposes the MCP tool service, not a remote dashboard or SSE endpoint.
  Requests use stateless JSON MCP, without an initialization/session handshake.

## Enable

Install the released broker code first. For Homebrew-managed Pi installations,
update the published module through `pi-shared update --modules-only`; do not edit
installed module files or point production at a development checkout. Use the
operator in the installed module (normally
`~/.local/share/pi-shared/modules/local_web_search/scripts/local-search`). For a
standalone installation, use that installation's `scripts/local-search`.

1. Obtain the exact hostname from `tailscale status --json` → `Self.DNSName`,
   removing the terminal dot. Verify `tailscale serve status --json` has no
   enabled `AllowFunnel` entry on port 443. HTTPS certificates must be enabled.
2. Check that the ingress port is unused (`lsof -nP -iTCP:8891 -sTCP:LISTEN`).
   Port 8890 is normally used by browser-worker, not this ingress.
3. Persist the ingress configuration and install both LaunchAgents. For managed
   Pi installations, use **pi-shared 0.1.20+** so updates and uninstall track both
   services. Preview first, then replace `--plan` with `--yes`:

   ```bash
   MCP_TAILNET_HOST=your-server.your-tailnet.ts.net MCP_TAILNET_PORT=8891 \
     pi-shared setup --plan
   ```

   For standalone installations only:

   ```bash
   MCP_TAILNET_HOST=your-server.your-tailnet.ts.net MCP_TAILNET_PORT=8891 \
     scripts/local-search install
   ```

   Managed updates preserve recorded ingress settings, ignoring shell overrides.
   Use explicit setup to change/disable them. An independently created ingress
   not recorded in the managed receipt blocks update/uninstall rather than being
   silently adopted or left running.

4. Add/replace **only** the search route; do not reset other Serve handlers:

   ```bash
   tailscale serve --bg --https=443 --set-path=/local-search/mcp \
     http://127.0.0.1:8891/mcp
   tailscale serve status --json
   ```

5. From a permitted tailnet machine, check real HTTPS (do not use `curl -k`):

   ```bash
   curl --fail-with-body --max-time 15 \
     https://your-server.your-tailnet.ts.net/local-search/mcp \
     -H 'Content-Type: application/json' -H 'Accept: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
   ```

The result must contain `web_search` and `web_fetch`. A `GET` to the same URL
returns 405 by design; pasting it into a browser is not a compatibility test.
The local `scripts/local-search verify` tests the ingress backend but cannot
prove HTTPS, remote reachability, or the tailnet access policy.

## Clients

For Pi setup (0.1.17+):

```bash
pi-shared setup --search existing \
  --search-url https://your-server.your-tailnet.ts.net/local-search/mcp
```

No `--search-key-file` is needed. `existing` means a compatible MCP endpoint,
not a raw Brave/Google/Tavily API. The Pi client requires direct JSON MCP calls
to `web_search(query, num_results)` and `web_fetch(url, max_chars)`; transports
requiring SSE or a negotiated MCP session need an adapter.

## Operations and rollback

- `scripts/local-search status`, `verify`, `restart`, `stop`, and `start` cover
  both installed LaunchAgents. Tailscale Serve is configured separately.
- `scripts/local-search logs tailnet` reads the ingress log (private, no access
  logging). It uses the same private provider keys, cache, and SQLite telemetry
  as the local broker. In-memory limits/circuit breakers are per process, not
  shared quota enforcement.
- `restart` does not persist changed environment variables; use `install`.
- Remove just the route, then disable/remove the optional ingress:

  ```bash
  tailscale serve --https=443 --set-path=/local-search/mcp off
  MCP_TAILNET_HOST='' pi-shared setup --yes
  # Standalone installation instead: MCP_TAILNET_HOST='' scripts/local-search install
  ```

SSH forwarding and local consumers remain supported. Default/fresh installs
have no ingress process because `MCP_TAILNET_HOST` is empty.
