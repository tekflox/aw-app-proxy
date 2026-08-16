---
repo: architecture
path: docs/architecture/aw-app-proxy.md
source: generated
edited: false
checksum: sha256:72bf61cb6c75f1c091860edda70d13158bd42da10716bcc5a4912ed26f174551
---
# Proxy

- **repo**: aw-app-proxy
- **layer**: app
- **technologies**: python
- **health** (derived): planned

Browser cookie proxy: an HTTP CONNECT proxy the AW Browser tunnels through, plus the cookie-sync API (/sync-cookies, /clear-cookies) that the aw-sync browser extensions (Chrome + iOS/Safari) push the user's real-browser cookies through, and encrypted persistence of chosen cookies in Postgres so they survive a proxy restart. aw-app-browser depends on this app for authentication.

## Connections
- `db` → **postgres** — app-owned tables in the workspace schema
- `http` → **aw-workspace** — routes mounted at /api/apps/proxy

## MCP tools
_none exposed_

## Requirements
_none documented_
