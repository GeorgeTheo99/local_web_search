# ADR 0001: Remote MCP access through loopback SSH forwarding

- **Status:** Accepted
- **Date:** 2026-07-16

## Context

The MCP broker has no native client authentication and provides search plus a
public-web fetch tool. It is intentionally bound to `127.0.0.1:8889`; SearXNG is
bound to `127.0.0.1:8888`. Remote Pi and other MCP clients need reliable access
without turning either service into an unauthenticated network endpoint.

Generic transport and local-search security belong in this repository because
they must evolve with the broker endpoint and its operating model. Pi-specific
provider policy and Pi configuration remain in `pi-shared`. This repository does
not edit a client's `~/.pi` configuration.

## Decision

Support remote clients with a persistent macOS LaunchAgent that runs an SSH
local forward:

```text
client 127.0.0.1:8889
  -> authenticated SSH connection
  -> server 127.0.0.1:8889
```

The client endpoint remains `http://127.0.0.1:8889/mcp`. The broker and SearXNG
retain loopback-only binds. `scripts/local-search-tunnel` generates and manages
the secret-free client LaunchAgent; SSH host, user, key, host-key policy, and
network route stay in the client's SSH configuration.

## Options considered

| Option | Authentication and exposure | Operations | Decision |
|---|---|---|---|
| Persistent SSH local forward | OpenSSH authenticates and encrypts; broker remains loopback-only | Small LaunchAgent; reconnects after login/network loss | **Selected** |
| Direct Tailscale listener | Tailnet identity limits reachability, but every permitted tailnet peer can reach an otherwise unauthenticated broker unless application ACLs/auth are added | Simple path, larger blast radius and new listener | Rejected for current broker |
| Caddy + Cloudflare Access | Access protects the public hostname, but direct `:8080` routes can bypass Access and Pi does not follow the interactive auth redirect | Adds proxy/header/auth coupling; redirect incompatibility | Rejected |
| Native authenticated remote MCP | Can provide per-client identity, authorization, quotas, and audit | Requires an auth protocol, key lifecycle, TLS/reverse-proxy design, and direct-exposure hardening | Deferred until multiple-client needs justify it |

Tailscale may carry the SSH packets, but it is not the MCP authorization layer.
No MCP route is added to Caddy, the LAN, or a Tailscale listener.

## Security model

### Protected assets

- Search terms, fetched URLs, and returned content.
- Availability of the configured Brave or local SearXNG provider and the broker host.
- The client's dedicated SSH private key.

### Trust boundaries and controls

- The MCP listener trusts processes that can connect to loopback on the server.
- A remote client crosses that boundary only after SSH public-key authentication.
- The tunnel listens on client loopback only, so it is not a LAN service.
- A dedicated key should use `restrict`, re-enable only port forwarding, and
  limit `permitopen` to `127.0.0.1:8889`. A forced `/usr/bin/false` command blocks
  command sessions while `ssh -N` requests no session.
- For strict prevention of reverse (`-R`) forwarding, use a dedicated server
  account with `AllowTcpForwarding local`; `permitopen` alone constrains `-L`
  destinations but not every other forwarding type.
- HTTP Host and browser Origin checks accept only exact loopback authorities.
- LaunchAgent plists, active logs, archived logs, and telemetry use private
  permissions. Operational URL query values and known query-bearing summary
  paths are redacted.

### Residual risks

- Any process running as the same user on the client can call the local tunnel.
  Any process running as the service user on the server can call the broker.
- A stolen SSH key remains usable within its server-side restrictions. Use a
  dedicated key, protect it with the macOS keychain/passphrase where compatible
  with unattended login, and revoke it promptly if the laptop is lost.
- There is no application-level per-client rate limit. SSH identity plus
  loopback scope limits callers, while backend deadlines, result caps, response
  byte limits, PDF bounds, and a two-job PDF semaphore limit individual work.
  Add authenticated quotas before any direct network exposure.
- `web_fetch` validates DNS answers before asking HTTPX to connect, so DNS can
  change between validation and connection. Fixing this TOCTOU gap requires a
  resolver/transport that pins a validated address while preserving TLS SNI and
  certificate verification for every redirect. This is deferred, not considered
  safe for direct exposure, and is tracked as a hardening prerequisite.
- Existing Caddy `:80`/`:8080` SearXNG routes are separate from MCP and may be
  reachable directly on local/tailnet interfaces. This ADR does not endorse or
  change those routes; they require review in the Caddy-owning project.

## Consequences

- Remote support is reliable without widening the broker listener.
- Each client needs an SSH config entry, host-key enrollment, and a restricted
  key installed server-side.
- Interactive Cloudflare Access is not in the MCP path.
- Direct MCP proxying/listening remains prohibited until native authentication,
  authorization, rate limits, address-pinned SSRF defense, proxy header policy,
  and exposure tests are designed and reviewed.
