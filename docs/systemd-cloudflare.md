# Standalone systemd deployment behind Cloudflare Tunnel

This pattern runs the credential-based Streamable HTTP server as a single-user service. It is
appropriate when the connector and Nextcloud share a host, the MCP client is a service rather
than an interactive user, and the account and file root are deliberately fixed.

It is not the per-user OAuth deployment. Every accepted MCP request acts as the one Nextcloud
account named by `NC_MCP_USER`.

## Trust boundaries

Use all three layers:

1. Bind Uvicorn to loopback so the origin is not reachable from the LAN.
2. Protect the public hostname with a Cloudflare Access Service Auth policy and a dedicated
   service token.
3. Require `NC_MCP_STATIC_BEARER` at the connector as an independent origin credential.

The client sends `CF-Access-Client-Id` and `CF-Access-Client-Secret` for Cloudflare Access, plus
`Authorization: Bearer ...` for the connector. Do not reuse any of those values between
customers or services.

## Install

Install `uv`, clone a trusted revision, and create the environment with the project's supported
Python version:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/street1983nk/nextcloud-mcp-connector.git
cd nextcloud-mcp-connector
~/.local/bin/uv python install 3.13
~/.local/bin/uv sync --python 3.13 --all-extras
```

Pin a release or reviewed commit in production. Do not run a moving branch without validating
it first.

Create `/etc/nextcloud-mcp/environment` as a root-owned mode `0600` file:

```dotenv
NC_MCP_URL=http://localhost:11000
NC_MCP_USER=service-account
NC_MCP_APP_PASSWORD=REPLACE_WITH_A_DEDICATED_APP_PASSWORD
NC_MCP_STATIC_BEARER=REPLACE_WITH_A_LONG_RANDOM_VALUE
NC_MCP_PUBLIC_URL=https://mcp.example.com
NC_MCP_ALLOWED_HOSTS=mcp.example.com,127.0.0.1,localhost
NC_MCP_FILES_ROOT=/AI
NC_MCP_TALK_SEND=false
PYTHONDONTWRITEBYTECODE=1
```

`localhost:11000` is an example Nextcloud AIO loopback origin. Use the actual private origin
for your deployment. Create the configured files root before starting. The root only confines
file tools; calendar, contacts, Deck, Mail, Notes, Tables and Talk continue to use the service
account's Nextcloud permissions.

An explicit `NC_MCP_ALLOWED_HOSTS` replaces the default loopback entries, so include the names
used by local protocol checks as well as the external hostname. Keep `NC_MCP_TALK_SEND=false`
unless outbound messaging has been separately reviewed.

## systemd unit

Create `/etc/systemd/system/nextcloud-mcp.service`, adjusting the user and checkout paths:

```ini
[Unit]
Description=Nextcloud MCP Connector
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
User=nextcloud-mcp
Group=nextcloud-mcp
WorkingDirectory=/opt/nextcloud-mcp-connector
EnvironmentFile=/etc/nextcloud-mcp/environment
ExecStart=/opt/nextcloud-mcp-connector/.venv/bin/uvicorn mcp_connector.entry_http:app --host 127.0.0.1 --port 8765
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=read-only
ProtectControlGroups=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectClock=true
RestrictSUIDSGID=true
RestrictRealtime=true
LockPersonality=true
MemoryDenyWriteExecute=true
SystemCallArchitectures=native
UMask=0077

[Install]
WantedBy=multi-user.target
```

If Nextcloud is not managed by Docker, remove `Requires=docker.service` and the matching
`After` entry. Then enable the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nextcloud-mcp.service
curl -fsS http://127.0.0.1:8765/health
```

The health route is intentionally unauthenticated at the origin. It remains private because
Uvicorn listens only on loopback. If Cloudflare Access protects `mcp.example.com/*`, the public
health URL should return Access `403` without service-token headers.

## Cloudflare Tunnel and Access

Add a remotely managed tunnel ingress before the catch-all rule:

```text
mcp.example.com -> http://127.0.0.1:8765
```

Create a proxied CNAME whose target is `<TUNNEL-UUID>.cfargotunnel.com`. The target is
Cloudflare's stable routing name for that tunnel; do not expose the loopback port or create a
LAN firewall rule.

Create a self-hosted Access application for `mcp.example.com/*`, a dedicated service token,
and a **Service Auth** policy that includes only that token. An Access policy and a tunnel
route do not replace the DNS record; all three must exist.

Expected unauthenticated checks:

```bash
curl -o /dev/null -sS -w '%{http_code}\n' https://mcp.example.com/health
curl -o /dev/null -sS -w '%{http_code}\n' -X POST https://mcp.example.com/mcp
```

Both should return `403` from Cloudflare Access and should produce no request line in the
connector journal.

## Validate the protocol

A health response proves liveness only. Complete validation requires an authenticated MCP
`tools/list` and at least one harmless tool call, such as `files_list` against the configured
root. Test through the public hostname with all three client headers before connecting an
agent.

Never put the app password, MCP bearer, or Cloudflare service-token secret in a repository,
command-line argument, shell history, support transcript, or log. Rotate each credential
independently when access changes.
