// AW Cookie Sync popup logic.
//
// The user configures a "sync host" (e.g. aw.tekflox.com) which is persisted
// in chrome.storage.local so it survives browser restarts. The actual POST
// targets are built from that host:
//   bare hostname          → https://<host>/<endpoint>
//   localhost / 127.x      → http://<host>/<endpoint>   (auto-downgrade)
//   full URL with scheme   → honored as-is, /<endpoint> appended

const DEFAULT_HOST = "aw.tekflox.com"; // Default host — configure in extension settings
const STORAGE_KEY = "awSyncHost";

const statusEl = document.getElementById("status");
const statsEl = document.getElementById("stats");
const domainsEl = document.getElementById("domains");
const hostInput = document.getElementById("host");
const hostHint = document.getElementById("host-hint");
const resetBtn = document.getElementById("reset-host");

function setStatus(msg, type = "") {
  statusEl.textContent = msg;
  statusEl.className = "status " + type;
}

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

function buildProxyUrl(rawHost) {
  return buildEndpointUrl(rawHost, "/sync-cookies");
}

function buildClearUrl(rawHost) {
  return buildEndpointUrl(rawHost, "/clear-cookies");
}

function updateHint() {
  hostHint.textContent = `→ ${buildProxyUrl(hostInput.value)}`;
}

async function loadHost() {
  try {
    const stored = await chrome.storage.local.get(STORAGE_KEY);
    hostInput.value = stored[STORAGE_KEY] || DEFAULT_HOST;
  } catch (e) {
    hostInput.value = DEFAULT_HOST;
  }
  updateHint();
}

async function saveHost(value) {
  const v = (value || "").trim() || DEFAULT_HOST;
  try {
    await chrome.storage.local.set({ [STORAGE_KEY]: v });
  } catch (e) {
    // ignore — sync still works using the in-memory value for this popup
  }
}

hostInput.addEventListener("input", updateHint);
hostInput.addEventListener("change", () => saveHost(hostInput.value));
hostInput.addEventListener("blur", () => saveHost(hostInput.value));

resetBtn.addEventListener("click", async () => {
  hostInput.value = DEFAULT_HOST;
  await saveHost(DEFAULT_HOST);
  updateHint();
  setStatus(`Reset to ${DEFAULT_HOST}`, "success");
});

async function getCookiesForDomains(domains) {
  const allCookies = [];
  const seen = new Set();

  for (const domain of domains) {
    const cookies = await chrome.cookies.getAll({ domain });
    for (const c of cookies) {
      const key = `${c.domain}|${c.name}|${c.path}`;
      if (!seen.has(key)) {
        seen.add(key);
        allCookies.push(c);
      }
    }
  }
  return allCookies;
}

async function getAllCookiesForCurrentTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.url) return [];

  const url = new URL(tab.url);
  const domain = url.hostname;
  const domains = [domain];
  const parts = domain.split(".");
  for (let i = 1; i < parts.length - 1; i++) {
    domains.push(parts.slice(i).join("."));
  }

  return getCookiesForDomains(domains);
}

// Custom error so the catch block can show a friendly message + open-host
// hint instead of a generic "Error: ...".
class NotLoggedInError extends Error {
  constructor(host) {
    super(`Not logged in to ${host}`);
    this.host = host;
  }
}

// AW's auth cookie. SameSite=Lax means a cross-site fetch from this popup
// won't carry it automatically — so we read the value via chrome.cookies
// (the extension has the `cookies` permission) and forward it as a header.
const AW_COOKIE = "aw_id_jwt"; // aw-workspace IdentityGuard's apex cookie (F2) — not the legacy "aw_jwt"

function originForHost(rawHost) {
  const host = (rawHost || "").trim();
  if (!host) return `https://${DEFAULT_HOST}`;
  if (/^https?:\/\//i.test(host)) {
    // strip path / trailing slash, keep just origin
    try { return new URL(host).origin; } catch (_) { return host.replace(/\/+$/, ""); }
  }
  const isPlainHttp = /^(localhost|127\.|0\.0\.0\.0|\[::1\])/i.test(host);
  return `${isPlainHttp ? "http" : "https"}://${host}`;
}

async function getAwJwt(rawHost) {
  const origin = originForHost(rawHost);
  try {
    const c = await chrome.cookies.get({ url: origin, name: AW_COOKIE });
    return c?.value || null;
  } catch (_) {
    return null;
  }
}

async function authedPost(url, body) {
  const token = await getAwJwt(hostInput.value);
  if (!token) {
    throw new NotLoggedInError(hostInput.value || DEFAULT_HOST);
  }
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${token}`,
    },
    body: JSON.stringify(body),
  });
  if (resp.status === 401) {
    throw new NotLoggedInError(hostInput.value || DEFAULT_HOST);
  }
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
    name: c.name,
    value: c.value,
    domain: c.domain,
    path: c.path || "/",
    secure: c.secure || false,
    httpOnly: c.httpOnly || false,
    sameSite:
      c.sameSite === "no_restriction" ? "None"
      : c.sameSite === "lax" ? "Lax"
      : c.sameSite === "strict" ? "Strict"
      : "Lax",
    expirationDate: c.expirationDate || null,
  }));

  // Persist whatever's in the host box right now before firing.
  await saveHost(hostInput.value);
  const proxyUrl = buildProxyUrl(hostInput.value);

  const result = await authedPost(proxyUrl, { cookies: mapped });
  return readSyncResult(result);
}

async function postClear(payload) {
  await saveHost(hostInput.value);
  const url = buildClearUrl(hostInput.value);
  const result = await authedPost(url, payload);
  return {
    purged: result.purged ?? 0,
    cleared: result.cleared ?? 0,
    failed: result.failed ?? 0,
    browserReachable: result.browser_reachable !== false,
  };
}

function showNotLoggedIn(err) {
  const host = err.host || hostInput.value || DEFAULT_HOST;
  const origin = originForHost(host);
  setStatus(`Not logged in to ${host}. Open ${origin} in a tab and log in first.`, "error");
}

document.getElementById("sync-current").addEventListener("click", async () => {
  const btn = document.getElementById("sync-current");
  btn.disabled = true;
  setStatus("Reading cookies for current tab...");

  try {
    const cookies = await getAllCookiesForCurrentTab();
    setStatus(`Found ${cookies.length} cookies. Injecting...`);

    if (cookies.length === 0) {
      setStatus("No cookies for this domain.", "error");
      btn.disabled = false;
      return;
    }
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
    // We can't enumerate server-side cookies remotely, but the extension can
    // tell the server which tuples to delete based on what THIS browser sees
    // for the current tab's domain. Same lookup used by Sync.
    const cookies = await getAllCookiesForCurrentTab();
    if (cookies.length === 0) {
      setStatus("No cookies for this domain to clear.", "error");
      return;
    }
    const tuples = cookies.map((c) => ({
      name: c.name,
      domain: c.domain,
      path: c.path || "/",
    }));
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
  if (!confirm("Wipe ALL cookies on the server? This cannot be undone.")) {
    return;
  }
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

loadHost();
