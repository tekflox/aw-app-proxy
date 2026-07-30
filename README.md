# aw-app-proxy

Workspace app that provides authenticated browsing support for AW Browser.
It includes the CONNECT proxy, encrypted cookie persistence, and browser
extensions used to sync cookies into the workspace.

## Features

- HTTP CONNECT proxy for browser traffic.
- `/sync-cookies` and `/clear-cookies` endpoints for cookie injection and
  cleanup.
- CDP helpers for `Network.setCookie`, `Network.getAllCookies`,
  `Network.clearBrowserCookies`, and `Network.deleteCookies`.
- Persistent encrypted cookie storage.
- Chrome and Safari Web Extension sources under `extensions/`.

## Runtime Shape

The app runs in-process and starts the CONNECT proxy as a managed subprocess
through `ctx.services`. Route handlers use `ctx.db` and `ctx.secrets`; the
subprocess uses direct workspace imports so it can access the same database
tables and secret-store file without a framework context object.

## Configuration

`browser_cdp_list_url` controls the CDP `/json/list` endpoint used for cookie
injection and cleanup. Keep it configurable so the workspace can point the
proxy at the browser runtime used in that environment.

## Layout

```text
aw-app.json                        manifest
proxy_app/
  plugin.py                        registers routes and managed service
  proxy_server.py                  CONNECT tunnel and cookie-sync HTTP server
  routes.py                        cookie keys, persistent cookies, downloads
  cookie_store.py                  persisted cookie table access
  crypto.py                        Fernet encrypt/decrypt helpers
  cdp.py                           raw-socket CDP client
extensions/
  aw-sync-chrome/                  Chrome extension
  aw-sync-ios/                     Safari Web Extension and host app
windows/main.json                  declarative settings window
tests/
  validate_manifest.py             manifest and window checks
  test_cookie_store.py             cookie-store unit tests
```

## Testing

```bash
.venv/aw/bin/python tests/validate_manifest.py
.venv/aw/bin/python -m pytest tests/test_cookie_store.py -q
```

Live CONNECT and CDP behavior should be verified in a running workspace with
an available browser CDP endpoint.
