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

function buildEndpointUrl(rawHost, endpoint) {
  const host = (rawHost || "").trim();
  const path = endpoint.startsWith("/") ? endpoint : `/${endpoint}`;
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
  return buildEndpointUrl(rawHost, "/api/apps/proxy/sync-cookies");
}

function buildClearUrl(rawHost) {
  return buildEndpointUrl(rawHost, "/api/apps/proxy/clear-cookies");
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
  const result = await resp.json().catch(() => ({}));
  if (result.error) throw new Error(result.message || result.error);
  return result;
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
  return { injected: result.injected || 0, failed: result.failed || 0 };
}

async function postClear(payload) {
  await saveHost(hostInput.value);
  const url = buildClearUrl(hostInput.value);
  const result = await authedPost(url, payload);
  return { cleared: result.cleared ?? 0, failed: result.failed ?? 0 };
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

    const { injected, failed } = await injectCookies(cookies);
    setStatus(`Done: ${injected} synced, ${failed} failed`, injected > 0 ? "success" : "error");
    statsEl.textContent = `${cookies.length} cookies read, ${injected} injected into container`;
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
    const { cleared, failed } = await postClear({ cookies: tuples });
    setStatus(
      `Cleared ${cleared} cookies${failed ? `, ${failed} failed` : ""}.`,
      cleared > 0 ? "success" : "error",
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
    const { cleared, failed } = await postClear({ all: true });
    // cleared === -1 is the proxy.py sentinel meaning "all-at-once succeeded"
    // (Network.clearBrowserCookies doesn't return a count).
    if (failed > 0 && cleared <= 0) {
      setStatus("Failed to clear cookies.", "error");
    } else if (cleared === -1) {
      setStatus("All cookies cleared on server.", "success");
    } else {
      setStatus(`Cleared ${cleared} cookies.`, "success");
    }
  } catch (e) {
    if (e instanceof NotLoggedInError) showNotLoggedIn(e);
    else setStatus(`Error: ${e.message}`, "error");
  } finally {
    btn.disabled = false;
  }
});

loadHost();
