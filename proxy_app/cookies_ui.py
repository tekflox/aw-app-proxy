"""The persisted-cookies viewer served into the Settings panel's ``iframe``
widget.

Ports agentic-workspace's ``src/app/src/components/ProxyTab.jsx`` (live
browser cookies + a "persist to Postgres" action) onto this app's own
``/api/apps/proxy`` routes, and adds the DB-side counterpart ProxyTab never
had: a per-row "Forget" action calling ``DELETE /persistent-cookies/{name}``
so a cookie can actually be removed from the database, not just cleared from
the live browser. ``windows/main.json`` already declared exactly this
Persist/Forget pair as a ``table`` widget's ``row_actions`` — that widget type
was never implemented by aw-workspace-ui's declarative renderer (see
aw-app-tunnel's ``tunnels_ui.py`` for the same situation), so this page is
that dead spec made real.

Same layout constraint as aw-app-tunnel's tunnels_ui.py: the host renders
this in `.appwin-iframe`, a narrow `min-height: 320px` box inside the
Settings sidebar — one card per cookie, stacked, not a multi-column table.
"""
from __future__ import annotations

COOKIES_UI_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cookies</title>
<style>
  :root {
    color-scheme: dark light;
    --accent: var(--color-accent, #f5a623);
    --line: var(--color-border, rgba(128,128,128,.28));
    --muted: var(--color-text-muted, #64748b);
    --panel: rgba(128,128,128,.06);
  }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 12px; font: 13px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
         background: transparent; color: inherit; }

  .hint { font-size: 11px; color: var(--muted); margin: 0 0 10px; line-height: 1.5; }

  input[type=search] { font: inherit; font-size: 12px; padding: 6px 8px; border-radius: 6px;
                  border: 1px solid var(--line); background: rgba(128,128,128,.08);
                  color: inherit; width: 100%; margin-bottom: 10px; }
  input[type=search]:focus { outline: none; border-color: var(--accent); }

  .card { border: 1px solid var(--line); border-radius: 10px; padding: 8px 10px;
          margin-bottom: 6px; background: var(--panel); display: flex; align-items: center; gap: 8px; }
  .name-wrap { flex: 1; min-width: 0; }
  .name { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
          font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: block; }
  .domain { font-size: 11px; color: var(--muted); overflow: hidden; text-overflow: ellipsis;
            white-space: nowrap; display: block; }

  .badge { font-size: 10px; padding: 2px 6px; border-radius: 5px; flex: none;
           background: rgba(74,222,128,.15); color: #4ade80; font-weight: 600; }

  button { font: inherit; font-size: 11px; font-weight: 500; padding: 4px 10px;
           border-radius: 6px; border: 1px solid var(--line);
           background: transparent; color: inherit; cursor: pointer; flex: none;
           transition: background .12s, border-color .12s, color .12s; }
  button:hover { background: rgba(128,128,128,.16); border-color: rgba(128,128,128,.45); }
  button.primary { background: var(--accent); border-color: var(--accent);
                   color: #1a1205; font-weight: 600; }
  button.primary:hover { filter: brightness(1.08); background: var(--accent); }
  button.danger { color: #f87171; }
  button.danger:hover { background: rgba(248,113,113,.14); border-color: rgba(248,113,113,.45); }
  button:disabled { opacity: .4; cursor: default; }

  .msg { padding: 7px 10px; border-radius: 7px; font-size: 12px; margin-bottom: 10px; line-height: 1.45; }
  .msg.err { background: rgba(248,113,113,.13); color: #fca5a5; }
  .msg.ok  { background: rgba(74,222,128,.13); color: #86efac; }
  .empty { color: var(--muted); font-size: 12px; padding: 14px 0; text-align: center; }
</style>
</head>
<body>
<p class="hint">Cookies currently in the synced browser. <b>Persist</b> encrypts and stores a
cookie in Postgres so it survives a proxy restart; <b>Forget</b> removes it from the database.</p>
<div id="msg"></div>
<input type="search" id="search" placeholder="Filter by name or domain…">
<div id="list"></div>

<script>
const BASE = '/api/apps/proxy';
const $ = (id) => document.getElementById(id);
let keys = [];
let busy = null;

async function call(method, path) {
  const res = await fetch(BASE + path, { method, credentials: 'include' });
  let payload = {};
  try { payload = await res.json(); } catch (_e) {}
  if (!res.ok || payload.error) throw new Error(payload.error || payload.detail || ('HTTP ' + res.status));
  return payload;
}

function say(text, kind) {
  $('msg').innerHTML = text ? '<div class="msg ' + kind + '">' + text + '</div>' : '';
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function render() {
  const q = $('search').value.toLowerCase().trim();
  const filtered = q
    ? keys.filter((k) => k.name.toLowerCase().includes(q) || k.domain.toLowerCase().includes(q))
    : keys;

  if (!keys.length) {
    $('list').innerHTML = '<div class="empty">No cookies in the browser yet.</div>';
    return;
  }
  if (!filtered.length) {
    $('list').innerHTML = '<div class="empty">No cookies match "' + esc(q) + '".</div>';
    return;
  }

  $('list').innerHTML = filtered.map((k) => {
    const isBusy = busy === k.name;
    return '<div class="card">'
      + '<div class="name-wrap">'
      +   '<span class="name" title="' + esc(k.name) + '">' + esc(k.name) + '</span>'
      +   (k.domain ? '<span class="domain">' + esc(k.domain) + '</span>' : '')
      + '</div>'
      + (k.persisted ? '<span class="badge">DB</span>' : '')
      + (k.persisted
          ? '<button class="danger" data-forget="' + esc(k.name) + '" ' + (isBusy ? 'disabled' : '') + '>Forget</button>'
          : '<button class="primary" data-persist="' + esc(k.name) + '" ' + (isBusy ? 'disabled' : '') + '>Persist</button>')
      + '</div>';
  }).join('');
}

async function refresh() {
  const payload = await call('GET', '/cookie-keys');
  keys = payload.keys || [];
  if (payload.error) say(esc(payload.error), 'err');
  render();
}

$('list').addEventListener('click', async (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  const persistName = b.getAttribute('data-persist');
  const forgetName = b.getAttribute('data-forget');
  const name = persistName || forgetName;
  if (!name) return;
  busy = name;
  render();
  try {
    if (persistName) {
      await call('POST', '/persistent-cookies/' + encodeURIComponent(name));
      say('Persisted "' + esc(name) + '".', 'ok');
    } else {
      await call('DELETE', '/persistent-cookies/' + encodeURIComponent(name));
      say('Forgot "' + esc(name) + '" — removed from the database.', 'ok');
    }
  } catch (err) {
    say(esc(err.message), 'err');
  } finally {
    busy = null;
    await refresh();
  }
});

$('search').addEventListener('input', render);

refresh().catch((err) => say('Could not load cookies: ' + esc(err.message), 'err'));
</script>
</body>
</html>
"""
