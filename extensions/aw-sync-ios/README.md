# AW Sync Extension — Safari (iOS + macOS)

Same functionality as [`aw-sync-extension-chrome`](../aw-sync-extension-chrome): syncs Safari cookies into the AW container Chrome via the `/sync-cookies` and `/clear-cookies` proxy API.

---

## Build (requires a Mac with Xcode 14+)

```bash
cd tools/browser/aw-sync-extension-ios
bash setup.sh
# then open the generated Xcode project
open "AW Sync/AW Sync.xcodeproj"
```

`setup.sh` does three things:
1. Copies icons from the Chrome extension and generates a 128px variant.
2. Runs `xcrun safari-web-extension-converter` on the `extension/` source.
3. Replaces the boilerplate `ViewController.swift` files with custom ones that check extension state and deep-link into Settings.

---

## Directory layout

```
aw-sync-extension-ios/
├── setup.sh                   # Run on Mac to generate Xcode project
├── extension/                 # Safari Web Extension source
│   ├── manifest.json          # MV3, Safari-adapted
│   ├── popup.html             # Mobile-responsive dark-theme popup
│   ├── popup.js               # Identical logic to Chrome version
│   └── images/
│       ├── icon-16.png        # ← copied from Chrome ext by setup.sh
│       ├── icon-48.png        # ← copied from Chrome ext by setup.sh
│       └── icon-128.png       # ← generated from 48px via sips by setup.sh
└── native/
    ├── iOS/ViewController.swift    # Guides user to enable in Settings
    └── macOS/ViewController.swift  # Guides user to enable in Safari prefs
```

---

## Native "Sync All Cookies" (host app — bypasses HttpOnly restriction)

The Safari Web Extension popup (`popup.js`) uses `chrome.cookies.getAll()`, which **cannot read HttpOnly cookies** — the same restriction that applies to any browser extension on iOS. Auth cookies (like `aw_jwt`) are always `HttpOnly; Secure`, so the extension sees 0 cookies and shows *"No cookies for this domain."*

The iOS **host app** uses `WKHTTPCookieStore` from the shared `WKWebsiteDataStore.default()`, which has full access to Safari's cookie store — including `HttpOnly` cookies. This is available since iOS 11.

**How to use:**

1. Open the **AW Sync** host app (not the Safari extension popup).
2. The **AW Host** field is pre-filled from `UserDefaults` (`awSyncHost` key, default `aw.tekflox.com`). Edit if needed.
3. Optionally enter a **Domain Filter** to send only cookies matching a substring (blank = all cookies).
4. Tap **Sync All Cookies**.
   - The app reads every cookie via `WKHTTPCookieStore.getAllCookies`.
   - It finds the `aw_jwt` cookie for the configured host to use as the auth token.
   - POSTs all cookies as JSON to `https://<host>/sync-cookies` with header `X-AW-JWT: <token>`.
5. The status label shows `✅ Done: N read, M synced` on success.

> **Prerequisite:** Open `https://<your-aw-host>` in Safari and log in first so `aw_jwt` exists in the cookie store.

---

## What changed from the Chrome extension

| Area | Chrome | Safari |
|------|--------|--------|
| **Manifest** | MV3, no `tabs` | MV3 + `tabs` permission + `browser_specific_settings.safari` |
| **Icons** | `icon16.png`, `icon48.png` | `images/icon-{16,48,128}.png` |
| **popup.html** | Fixed 320px width | Responsive (`min 280px`, `max 400px`) + `inputmode="url"` |
| **`window.confirm()`** | Works | Replaced with inline overlay (may be suppressed in extension popups on iOS) |
| **`chrome.*` APIs** | Native | Works — Safari aliases `chrome.*` → `browser.*` in Web Extensions |
| **Host app** | N/A | SwiftUI-free Swift host with extension-state check |

No changes to the core sync/clear logic or the server-side `proxy.py`.

---

## Enabling the extension

### iOS (Safari 15.4+, iPhone / iPad)

**Step 1 — Install the app**

The app must be installed on your device first. Three options:

- **Xcode (easiest for dev):** Connect your device via USB or Wi-Fi, select it as the run target in Xcode, and hit ▶ Run. You'll be prompted to trust the developer certificate once in **Settings → General → VPN & Device Management**.
- **TestFlight:** Upload an `.ipa` via App Store Connect and invite yourself.
- **App Store:** Only relevant after a public release.

**Step 2 — Enable the extension in Settings**

1. Open the **Settings** app.
2. Scroll down and tap **Safari**.
3. Tap **Extensions**.
4. Tap **AW Sync Extension**.
5. Toggle **Allow Extension** → ON.
6. Under **Permissions**, tap **All Websites** and select **Allow**.
   *(The "All Websites" permission is required so the extension can read cookies for any domain you're visiting.)*

**Step 3 — Use it in Safari**

1. Open **Safari** and navigate to any site you're logged into (e.g. `aw.tekflox.com`).
2. Tap the **Extensions button** (looks like a puzzle piece 🧩) in the Safari address bar.
   - If you don't see it: tap the **aA** button on the left side of the address bar → scroll to find the extension, or long-press the address bar area.
3. Tap **AW Sync Extension** — the popup appears as a bottom sheet.
4. Verify the **Sync host** field shows your AW hostname (e.g. `aw.tekflox.com`). Tap it to edit if needed.
5. Tap **Sync Current Tab Cookies** to push cookies into the container.

> **First-time permission prompt:** Safari may ask *"AW Sync Extension would like to access information from this website."* Tap **Allow for One Day** or **Always Allow** — the extension needs this to read the current tab's URL.

---

### macOS (Safari 15+)

**Step 1 — Build and run the host app**

You only need to do this once. The host app registers the extension with Safari.

```bash
# From the repo root:
cd tools/browser/aw-sync-extension-ios
bash setup.sh
open "AW Sync/AW Sync.xcodeproj"
```

In Xcode: select the **AW Sync (macOS)** scheme → ▶ Run. The app window opens and shows a button to open Safari Extension Preferences.

**Step 2 — Enable in Safari**

1. Open **Safari**.
2. In the menu bar: **Safari → Settings…** (or ⌘ ,).
3. Click the **Extensions** tab.
4. Find **AW Sync Extension** in the left list and tick its checkbox ✓.
5. When asked about website access, click **Always Allow on Every Website…** (or configure per-domain if you prefer).

The **AW** icon now appears in the Safari toolbar (right side of the address bar). If you don't see it, right-click the toolbar → **Customize Toolbar** and drag it in.

**Step 3 — Use it**

1. Navigate to the site whose cookies you want to sync (must be logged in there so the `aw_jwt` cookie exists).
2. Click the **AW** toolbar icon — the popup opens.
3. Confirm the **Sync host** and click **Sync Current Tab Cookies**.

---

### Troubleshooting

| Symptom | Fix |
|---------|-----|
| Extension popup shows "No cookies for this domain." | This is expected — `chrome.cookies.getAll()` can't read `HttpOnly` cookies on iOS. Use the **host app's "Sync All Cookies"** button instead. |
| Host app: "No aw_jwt cookie found" | Open `https://<your-host>` in Safari and log in first, then retry. |
| Host app: 401 Unauthorized | Your session expired on the AW server — log in again from Safari. |
| Extension not visible in Safari address bar (iOS) | Settings → Safari → Extensions → AW Sync Extension → make sure toggle is ON |
| macOS: toolbar icon missing | Safari → Settings → Extensions → tick the checkbox; right-click toolbar → Customize Toolbar |
| iOS: "Untrusted Developer" on launch | Settings → General → VPN & Device Management → tap your Apple ID → Trust |

---

## Bundle ID / signing

The default bundle ID is `com.tekflox.aw-sync`. Override it when running `setup.sh`:

```bash
BUNDLE_ID=com.example.aw-sync bash setup.sh
```

You need an Apple Developer account (free or paid) to install on a real iOS device.
