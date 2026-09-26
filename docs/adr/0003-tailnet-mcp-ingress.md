# ADR 0003: Optional MCP-only Tailscale Serve ingress

Status: accepted. Extends ADR 0001; SSH forwarding remains supported.

## Decision

Support URL-only clients when an operator explicitly trusts all devices allowed
by their tailnet access rules. Tailscale Serve provides HTTPS and network
admission; the broker does not add per-user identity checks or bearer keys.
Tagged devices are supported. Public Funnel is prohibited on the serving port.

Keep the existing local broker and its Host/Origin policy unchanged. Start a
second loopback-only process only when `MCP_TAILNET_HOST` is set. Its ingress
permits only `POST /mcp` for that exact MagicDNS hostname and an absent or exact
same-site HTTPS Origin. Diagnostics are never reachable through this listener,
including requests with spoofed loopback Host or forwarded headers.

A separate listener is intentional: proxy requests originate from loopback too,
so mixing remote requests with a trusted-local exemption could expose diagnostics.
Both processes use stateless JSON FastMCP and the same private data directory.

## Consequences

- Default installs and SSH clients retain their existing behavior.
- Serve route management is explicit and preserves unrelated handlers.
- Local processes remain trusted. The broker cannot authenticate Serve vs a
  different loopback proxy or independently prove that Funnel is disabled.
- Operators must retain tailnet-only routing and account for device sharing.
- All permitted clients can spend provider quota; no per-client limits are added.
- In-memory caches, rate limits, and circuit breakers are per process; durable
  cache/SQLite telemetry are shared. The extra process is opt-in overhead.
- Installation, restart, stop, verification, and removal cover both processes.

See [configuration and rollback](../remote-mcp-over-tailnet.md).
