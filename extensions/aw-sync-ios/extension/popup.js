// AW Cookie Sync — Safari Web Extension popup logic.
//
// Adapted from tools/browser/aw-sync-extension-chrome/popup.js for Safari
// (iOS 15.4+ and macOS 12+). Uses the same chrome.* namespace that Safari
// aliases to browser.* in Web Extensions.
//
// The user configures a "sync host" (e.g. aw.tekflox.com) persisted in
// chrome.storage.local. Sync targets are built from that host:
//   bare hostname          → https://<host>/<endpoint>
//   localhost / 127.x      → http://<host>/<endpoint>   (auto-downgrade)
//   full URL with scheme   → honored as-is, /<endpoint> appended

const DEFAULT_HOST = "aw.tekflox.com"; // Default host — configure in extension settings
const STORAGE_KEY  = "awSyncHost";

const statusEl  = document.getElementById("status");
const statsEl   = document.getElementById("stats");
const domainsEl = document.getElementById("domains");
const hostInput = document.getElementById("host");
const hostHint  = document.getElementById("host-hint");
const resetBtn  = document.getElementById("reset-host");

// ── Confirm overlay (replaces window.confirm which may be blocked in Safari
//    Web Extension popups on iOS).
const confirmOverlay = document.getElementById("confirm-overlay");
const confirmMsg     = document.getElementById("confirm-msg");
const confirmOk      = document.getElementById("confirm-ok");
const confirmCancel  = document.getElementById("confirm-cancel");

function showConfirm(message) {
  return new Promise((resolve) => {
    confirmMsg.textContent = message;
    confirmOverlay.classList.add("active");
    const cleanup = () => confirmOverlay.classList.remove("active");
    confirmOk.onclick = () => { cleanup(); resolve(true);  };
    confirmCancel.onclick = () => { cleanup(); resolve(false); };
  });
}

// ── Status helpers ────────────────────────────────────────────────────────────

function setStatus(msg, type = "") {
  statusEl.textContent = msg;
  statusEl.className   = "status " + type;
}

// ── URL building ──────────────────────────────────────────────────────────────

function hostnameOf(rawHost) {
  const host = (rawHost || "").trim();
  if (!host) return DEFAULT_HOST;
  if (/^https?:\/\//i.test(host)) {
    try { return new URL(host).hostname; } catch (_) { return host; }
  }
  return host.split(":")[0].split("/")[0];
}

// Per-app subdomains (<app-slug>.app.<ws-slug>.workspace...) are Host()-routed
// straight to the app's sub-application with NO path prefix stripped (see
// aw-workspace's src/apps/runtime.py _attach_mount: Host(f"{app_id}.app.{{_}}")
// dispatches "/sync-cookies" as-is). The workspace-wide API host
// (api.<ws>.workspace...) mounts the same app under /api/apps/proxy instead
// (Mount(f"/api/apps/{app_id}")). Same backend view, two different URL shapes
// — pick the right one based on which host pattern is configured, instead of
// hardcoding the path-prefixed shape and silently 404ing on the subdomain.
function isAppSubdomain(rawHost) {
  return /\.app\./i.test(hostnameOf(rawHost));
}

function buildEndpointUrl(rawHost, name) {
  const host = (rawHost || "").trim();
  const bare = name.startsWith("/") ? name : `/${name}`;
  const path = isAppSubdomain(host) ? bare : `/api/apps/proxy${bare}`;
  if (!host) return `https://${DEFAULT_HOST}${path}`;

  // Full URL with scheme — honor it. Strip a trailing slash, then append path.
  if (/^https?:\/\//i.test(host)) {
    const trimmed = host.replace(/\/+$/, "");
    return trimmed.endsWith(path) ? trimmed : `${trimmed}${path}`;
  }

  // Bare hostname (optionally with :port). Localhost / 127.x / private LAN
  // typically isn't behind TLS, so default to http for those.
  const isPlainHttp = /^(localhost|127\.|0\.0\.0\.0|\[::1\])/i.test(host);
  const scheme = isPlainHttp ? "http" : "https";
  return `${scheme}://${host}${path}`;
}

function buildProxyUrl(rawHost) { return buildEndpointUrl(rawHost, "/sync-cookies"); }
function buildClearUrl(rawHost) { return buildEndpointUrl(rawHost, "/clear-cookies"); }

function updateHint() {
  hostHint.textContent = `→ ${buildProxyUrl(hostInput.value)}`;
}

// ── Storage ───────────────────────────────────────────────────────────────────

async function loadHost() {
  try {
    const stored = await chrome.storage.local.get(STORAGE_KEY);
    hostInput.value = stored[STORAGE_KEY] || DEFAULT_HOST;
  } catch (_) {
    hostInput.value = DEFAULT_HOST;
  }
  updateHint();
}

async function saveHost(value) {
  const v = (value || "").trim() || DEFAULT_HOST;
  try {
    await chrome.storage.local.set({ [STORAGE_KEY]: v });
  } catch (_) {
    // ignore — sync still works using the in-memory value for this popup
  }
}

hostInput.addEventListener("input",  updateHint);
hostInput.addEventListener("change", () => saveHost(hostInput.value));
hostInput.addEventListener("blur",   () => saveHost(hostInput.value));

resetBtn.addEventListener("click", async () => {
  hostInput.value = DEFAULT_HOST;
  await saveHost(DEFAULT_HOST);
  updateHint();
  setStatus(`Reset to ${DEFAULT_HOST}`, "success");
});

// ── Cookie helpers ────────────────────────────────────────────────────────────

// Safari 18+ (incl. iOS 26) regression: chrome.cookies.getAll() WITHOUT a storeId
// queries an inaccessible store and silently returns [] — even with "All Websites:
// Allow". The fix (Apple Dev Forums) is to enumerate stores via getAllCookieStores()
// and pass each storeId explicitly. https://developer.apple.com/forums/thread/768065
async function getCookieStoreIds() {
  try {
    const stores = await chrome.cookies.getAllCookieStores();
    const ids = (stores || []).map((s) => s.id).filter((id) => id != null);
    return ids.length ? ids : [undefined]; // fall back to the implicit default store
  } catch (_) {
    return [undefined];
  }
}

async function getCookiesForDomains(domains) {
  const allCookies = [];
  const seen = new Set();
  const storeIds = await getCookieStoreIds();
  for (const storeId of storeIds) {
    for (const domain of domains) {
      const query = storeId === undefined ? { domain } : { domain, storeId };
      let cookies = [];
      try { cookies = await chrome.cookies.getAll(query); } catch (_) { cookies = []; }
      for (const c of cookies) {
        const key = `${c.domain}|${c.name}|${c.path}`;
        if (!seen.has(key)) {
          seen.add(key);
          allCookies.push(c);
        }
      }
    }
  }
  return allCookies;
}

async function getAllCookiesForCurrentTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.url) return [];

  const url    = new URL(tab.url);
  const domain = url.hostname;
  const domains = [domain];
  const parts   = domain.split(".");
  // Walk up the domain hierarchy (e.g. api.github.com → github.com)
  for (let i = 1; i < parts.length - 1; i++) {
    domains.push(parts.slice(i).join("."));
  }
  return getCookiesForDomains(domains);
}

// ── Auth ──────────────────────────────────────────────────────────────────────

class NotLoggedInError extends Error {
  constructor(host) {
    super(`Not logged in to ${host}`);
    this.host = host;
  }
}

const AW_COOKIE = "aw_id_jwt"; // aw-workspace IdentityGuard's apex cookie (F2) — not the legacy "aw_jwt"

function originForHost(rawHost) {
  const host = (rawHost || "").trim();
  if (!host) return `https://${DEFAULT_HOST}`;
  if (/^https?:\/\//i.test(host)) {
    try { return new URL(host).origin; } catch (_) { return host.replace(/\/+$/, ""); }
  }
  const isPlainHttp = /^(localhost|127\.|0\.0\.0\.0|\[::1\])/i.test(host);
  return `${isPlainHttp ? "http" : "https"}://${host}`;
}

async function getAwJwt(rawHost) {
  const origin = originForHost(rawHost);
  // Same Safari storeId quirk as getCookiesForDomains — probe every store.
  const storeIds = await getCookieStoreIds();
  for (const storeId of storeIds) {
    try {
      const query = storeId === undefined
        ? { url: origin, name: AW_COOKIE }
        : { url: origin, name: AW_COOKIE, storeId };
      const c = await chrome.cookies.get(query);
      if (c?.value) return c.value;
    } catch (_) { /* try next store */ }
  }
  return null;
}

async function authedPost(url, body) {
  const token = await getAwJwt(hostInput.value);
  if (!token) throw new NotLoggedInError(hostInput.value || DEFAULT_HOST);

  const resp = await fetch(url, {
    method:  "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${token}`,
    },
    body: JSON.stringify(body),
  });
  if (resp.status === 401) throw new NotLoggedInError(hostInput.value || DEFAULT_HOST);

  // Don't silently swallow non-2xx / non-JSON responses into {} — that made
  // real failures (404 from a wrong route, 502 upstream, HTML error page,
  // etc.) render as "0 synced, 0 failed" with no clue why. Surface the raw
  // status + body so the user (and the "Error: ..." status line) can see
  // what actually happened.
  const rawBody = await resp.text();
  let result;
  try {
    result = rawBody ? JSON.parse(rawBody) : {};
  } catch (_) {
    throw new Error(`HTTP ${resp.status} ${resp.statusText}: ${rawBody.slice(0, 200) || "(empty/non-JSON response)"}`);
  }
  if (!resp.ok) {
    throw new Error(result.message || result.error || `HTTP ${resp.status} ${resp.statusText}`);
  }
  if (result.error) throw new Error(result.message || result.error);
  return result;
}

// ── Sync / Clear ──────────────────────────────────────────────────────────────

// The server persists every cookie unconditionally and only THEN tries to
// inject them into a live browser — aw-app-browser is a separate app that is
// very often simply stopped. Reporting `injected` alone painted a perfectly
// good sync red ("0 synced, 0 failed") in exactly that case, even though every
// cookie was safely stored and the proxy's reconnect loop would inject them
// the moment the browser came up. Report what was STORED; treat the live
// injection as the bonus step it actually is.
function readSyncResult(result) {
  return {
    persisted: result.persisted ?? 0,
    injected:  result.injected  || 0,
    failed:    result.failed    || 0,
    // Older proxy builds predate this field; absent means "assume reachable".
    browserReachable: result.browser_reachable !== false,
  };
}

function describeSync({ persisted, injected, failed, browserReachable }) {
  if (!browserReachable) {
    return {
      text: `Done: ${persisted} stored (browser offline — injected when it starts)`,
      ok:   persisted > 0,
    };
  }
  return {
    text: `Done: ${persisted} stored, ${injected} injected${failed ? `, ${failed} failed` : ""}`,
    ok:   persisted > 0 && failed === 0,
  };
}

async function injectCookies(cookies) {
  const mapped = cookies.map((c) => ({
    name:           c.name,
    value:          c.value,
    domain:         c.domain,
    path:           c.path || "/",
    secure:         c.secure    || false,
    httpOnly:       c.httpOnly  || false,
    sameSite:
      c.sameSite === "no_restriction" ? "None"
      : c.sameSite === "lax"          ? "Lax"
      : c.sameSite === "strict"       ? "Strict"
      : "Lax",
    expirationDate: c.expirationDate || null,
  }));

  await saveHost(hostInput.value);
  const result = await authedPost(buildProxyUrl(hostInput.value), { cookies: mapped });
  return readSyncResult(result);
}

async function postClear(payload) {
  await saveHost(hostInput.value);
  const result = await authedPost(buildClearUrl(hostInput.value), payload);
  return {
    purged: result.purged ?? 0,
    cleared: result.cleared ?? 0,
    failed: result.failed ?? 0,
    browserReachable: result.browser_reachable !== false,
  };
}

function showNotLoggedIn(err) {
  const host   = err.host || hostInput.value || DEFAULT_HOST;
  const origin = originForHost(host);
  setStatus(`Not logged in to ${host}. Open ${origin} in a tab and log in first.`, "error");
}

// ── Button handlers ───────────────────────────────────────────────────────────

document.getElementById("sync-current").addEventListener("click", async () => {
  const btn = document.getElementById("sync-current");
  btn.disabled = true;
  setStatus("Reading cookies for current tab...");
  try {
    const cookies = await getAllCookiesForCurrentTab();

    if (cookies.length === 0) {
      const stores = await getCookieStoreIds();
      setStatus(
        `No cookies returned (cookie stores seen: ${stores.length}). ` +
        `Reload the page in this tab, then retry.`,
        "error",
      );
      return;
    }

    setStatus(`Found ${cookies.length} cookies. Injecting...`);

    // Show per-domain breakdown
    const domainCounts = {};
    for (const c of cookies) {
      domainCounts[c.domain] = (domainCounts[c.domain] || 0) + 1;
    }
    domainsEl.innerHTML = Object.entries(domainCounts)
      .sort((a, b) => b[1] - a[1])
      .map(([d, n]) => `<div class="domain-row"><span>${d}</span><span class="count">${n}</span></div>`)
      .join("");
    domainsEl.style.display = "block";

    const res = await injectCookies(cookies);
    const { text, ok } = describeSync(res);
    setStatus(text, ok ? "success" : "error");
    statsEl.textContent = res.browserReachable
      ? `${cookies.length} cookies read, ${res.injected} injected into container`
      : `${cookies.length} cookies read, ${res.persisted} stored for the container`;
  } catch (e) {
    if (e instanceof NotLoggedInError) showNotLoggedIn(e);
    else setStatus(`Error: ${e.message}`, "error");
  } finally {
    btn.disabled = false;
  }
});

document.getElementById("clear-current").addEventListener("click", async () => {
  const btn = document.getElementById("clear-current");
  btn.disabled = true;
  setStatus("Clearing current tab's cookies on server...");
  try {
    const cookies = await getAllCookiesForCurrentTab();
    if (cookies.length === 0) {
      setStatus("No cookies for this domain to clear.", "error");
      return;
    }
    const tuples = cookies.map((c) => ({ name: c.name, domain: c.domain, path: c.path || "/" }));
    const { purged, cleared, failed, browserReachable } = await postClear({ cookies: tuples });
    setStatus(
      browserReachable
        ? `Cleared ${purged} stored, ${cleared} evicted from the browser${failed ? `, ${failed} failed` : ""}.`
        : `Cleared ${purged} stored cookies (browser offline).`,
      purged > 0 || cleared > 0 ? "success" : "error",
    );
  } catch (e) {
    if (e instanceof NotLoggedInError) showNotLoggedIn(e);
    else setStatus(`Error: ${e.message}`, "error");
  } finally {
    btn.disabled = false;
  }
});

document.getElementById("clear-all").addEventListener("click", async () => {
  const btn = document.getElementById("clear-all");
  const confirmed = await showConfirm("Wipe ALL cookies on the server? This cannot be undone.");
  if (!confirmed) return;

  btn.disabled = true;
  setStatus("Clearing every cookie on server...");
  try {
    // The stored count is authoritative — wiping the live browser session is
    // best-effort and returns no count of its own.
    const { purged, browserReachable } = await postClear({ all: true });
    setStatus(
      browserReachable
        ? `Cleared ${purged} stored cookies and wiped the live browser session.`
        : `Cleared ${purged} stored cookies (browser offline — nothing left to inject).`,
      "success",
    );
  } catch (e) {
    if (e instanceof NotLoggedInError) showNotLoggedIn(e);
    else setStatus(`Error: ${e.message}`, "error");
  } finally {
    btn.disabled = false;
  }
});

// ── Init ──────────────────────────────────────────────────────────────────────

loadHost();
