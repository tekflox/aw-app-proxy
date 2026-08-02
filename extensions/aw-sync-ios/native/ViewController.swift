// AW Sync — unified host app ViewController (iOS + macOS)
//
// iOS: Uses WKHTTPCookieStore to read ALL Safari cookies including HttpOnly,
//      then POSTs to /sync-cookies. Includes an on-screen debug console.
//
// macOS: Simple helper that opens Safari Extension preferences.

#if os(iOS)
import UIKit
import WebKit

// ── Persistent crash/debug log ────────────────────────────────────────────────
// The console must survive a crash so the trail is still readable after the app
// is relaunched from the home screen. We mirror every line into a file in the
// app's Documents dir and reload it on the next launch. Crash + signal handlers
// append a final marker so a hard crash (not just a Swift error) is captured too.

private let awDebugLogURL: URL =
    FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("aw-sync-debug.log")

// ── Remote logging ────────────────────────────────────────────────────────────
// Mirror the debug trail to the AW backend (same host used for cookie sync) so a
// crash-on-launch is visible server-side without the device — reuses the existing
// public POST /api/meta/log endpoint (writes to .tmp/meta_display/glasses.log),
// tagged sid="aw-sync" so it's greppable apart from the glasses app.
private var awRemoteLogBase = "https://dev.tekflox.com"

private func awPostRemote(_ events: [[String: Any]], sync: Bool) {
    guard let url = URL(string: "\(awRemoteLogBase)/api/meta/log"),
          let body = try? JSONSerialization.data(withJSONObject: ["events": events]) else { return }
    var req = URLRequest(url: url, timeoutInterval: sync ? 3 : 10)
    req.httpMethod = "POST"
    req.setValue("application/json", forHTTPHeaderField: "Content-Type")
    req.httpBody = body
    if sync {
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { _, _, _ in sem.signal() }.resume()
        _ = sem.wait(timeout: .now() + 3)
    } else {
        URLSession.shared.dataTask(with: req).resume()
    }
}

private func awRemoteEvent(_ msg: String, level: String = "info", data: [String: Any] = [:]) -> [String: Any] {
    var ev: [String: Any] = ["source": "ios_app", "level": level, "msg": msg,
                             "sid": "aw-sync", "ts": Date().timeIntervalSince1970 * 1000]
    if !data.isEmpty { ev["data"] = data }
    return ev
}

// Async-signal-safe append (POSIX only) — safe to call from a signal handler.
private func awAppendCrashRaw(_ text: String) {
    let fd = open(awDebugLogURL.path, O_WRONLY | O_APPEND | O_CREAT, 0o644)
    if fd >= 0 {
        _ = text.withCString { write(fd, $0, strlen($0)) }
        close(fd)
    }
}

// Top-level C-compatible signal handler (no captures → usable as a C func ptr).
private func awSignalHandler(_ sig: Int32) {
    let frames = Thread.callStackSymbols.prefix(25).joined(separator: "\n")
    awAppendCrashRaw("\n💥 SIGNAL \(sig) — hard crash\n\(frames)\n")
    signal(sig, SIG_DFL)
    raise(sig)
}

private var awCrashHandlersInstalled = false
private func awInstallCrashHandlers() {
    guard !awCrashHandlersInstalled else { return }
    awCrashHandlersInstalled = true
    NSSetUncaughtExceptionHandler { ex in
        let frames = ex.callStackSymbols.prefix(25).joined(separator: "\n")
        let text = "UNCAUGHT \(ex.name.rawValue): \(ex.reason ?? "?")\n\(frames)"
        awAppendCrashRaw("\n💥 \(text)\n")
        // Best-effort synchronous flush so the crash reaches the server before exit.
        awPostRemote([awRemoteEvent("crash_uncaught", level: "error", data: ["text": text])], sync: true)
    }
    [SIGABRT, SIGILL, SIGSEGV, SIGFPE, SIGBUS, SIGTRAP].forEach { signal($0, awSignalHandler) }
}

class ViewController: UIViewController {

    // MARK: - Config

    private let defaultHost = "proxy.app.aw.workspace.aw.tekflox.com"
    private let storageKey  = "awSyncHost"

    // safari-web-extension-converter's generated Main.storyboard binds this outlet
    // on launch. We build our UI programmatically and never touch `webView`, but the
    // property MUST exist — otherwise KVC throws NSUnknownKeyException and the app
    // crashes before viewDidLoad runs (the "crashes the moment I open it" symptom).
    @IBOutlet var webView: WKWebView!

    // MARK: - Subviews

    private let scrollView = UIScrollView()
    private let stackView: UIStackView = {
        let s = UIStackView()
        s.axis      = .vertical
        s.alignment = .fill
        s.spacing   = 16
        s.translatesAutoresizingMaskIntoConstraints = false
        return s
    }()

    private let titleLabel: UILabel = {
        let l = UILabel()
        l.text          = "AW Cookie Sync"
        l.font          = .systemFont(ofSize: 24, weight: .bold)
        l.textAlignment = .center
        return l
    }()

    private let subtitleLabel: UILabel = {
        let l = UILabel()
        l.text          = "Syncs Safari cookies (including HttpOnly) to your AW container."
        l.font          = .systemFont(ofSize: 14)
        l.textColor     = .secondaryLabel
        l.textAlignment = .center
        l.numberOfLines = 0
        return l
    }()

    private let hostField: UITextField = {
        let f = UITextField()
        f.placeholder            = "dev.tekflox.com"
        f.borderStyle            = .roundedRect
        f.autocapitalizationType = .none
        f.autocorrectionType     = .no
        f.keyboardType           = .URL
        f.returnKeyType          = .done
        f.clearButtonMode        = .whileEditing
        return f
    }()

    private let hostHintLabel: UILabel = {
        let l = UILabel()
        l.font      = .systemFont(ofSize: 12)
        l.textColor = .tertiaryLabel
        return l
    }()

    private let domainFilterField: UITextField = {
        let f = UITextField()
        f.placeholder            = "Domain filter (blank = all)"
        f.borderStyle            = .roundedRect
        f.autocapitalizationType = .none
        f.autocorrectionType     = .no
        f.keyboardType           = .URL
        f.returnKeyType          = .done
        f.clearButtonMode        = .whileEditing
        return f
    }()

    private lazy var syncButton: UIButton = {
        var config = UIButton.Configuration.filled()
        config.title               = "Sync All Cookies"
        config.cornerStyle         = .large
        config.baseBackgroundColor = .systemBlue
        let b = UIButton(configuration: config)
        b.addTarget(self, action: #selector(syncTapped), for: .touchUpInside)
        return b
    }()

    private lazy var settingsButton: UIButton = {
        var config = UIButton.Configuration.tinted()
        config.title       = "Open Safari Settings"
        config.cornerStyle = .large
        let b = UIButton(configuration: config)
        b.addTarget(self, action: #selector(openSettings), for: .touchUpInside)
        return b
    }()

    private let statusLabel: UILabel = {
        let l = UILabel()
        l.font          = .systemFont(ofSize: 14)
        l.textAlignment = .center
        l.numberOfLines = 0
        return l
    }()

    private let extensionStatusLabel: UILabel = {
        let l = UILabel()
        l.text          = "Enable the extension: Settings → Safari → Extensions → AW Sync"
        l.font          = .systemFont(ofSize: 13)
        l.textColor     = .tertiaryLabel
        l.textAlignment = .center
        l.numberOfLines = 0
        return l
    }()

    // MARK: - Debug console

    private let debugHeaderLabel: UILabel = {
        let l = UILabel()
        l.text      = "DEBUG LOG"
        l.font      = .systemFont(ofSize: 10, weight: .semibold)
        l.textColor = .secondaryLabel
        return l
    }()

    private lazy var copyLogButton: UIButton = {
        var config = UIButton.Configuration.gray()
        config.title       = "Copy"
        config.buttonSize  = .small
        config.cornerStyle = .medium
        let b = UIButton(configuration: config)
        b.addTarget(self, action: #selector(copyLogTapped), for: .touchUpInside)
        return b
    }()

    private lazy var clearLogButton: UIButton = {
        var config = UIButton.Configuration.gray()
        config.title       = "Clear"
        config.buttonSize  = .small
        config.cornerStyle = .medium
        let b = UIButton(configuration: config)
        b.addTarget(self, action: #selector(clearLogTapped), for: .touchUpInside)
        return b
    }()

    private let debugTextView: UITextView = {
        let t = UITextView()
        t.isEditable         = false
        t.isSelectable       = true   // allow manual copy/paste straight from the view
        t.font               = .monospacedSystemFont(ofSize: 11, weight: .regular)
        t.textColor          = .label
        t.backgroundColor    = UIColor.systemGray6
        t.layer.cornerRadius = 8
        t.textContainerInset = UIEdgeInsets(top: 8, left: 8, bottom: 8, right: 8)
        t.isScrollEnabled    = true
        return t
    }()

    private var debugLines: [String] = []
    private let logQueue = DispatchQueue(label: "com.tekflox.awsync.debuglog")

    private func timestamp() -> String {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss.SSS"
        return f.string(from: Date())
    }

    private func debugLog(_ msg: String) {
        let line = "[\(timestamp())] \(msg)"
        debugLines.append(line)
        print(line)
        logQueue.async {
            let data = (line + "\n").data(using: .utf8) ?? Data()
            if let h = try? FileHandle(forWritingTo: awDebugLogURL) {
                h.seekToEndOfFile(); h.write(data); try? h.close()
            } else {
                try? data.write(to: awDebugLogURL)
            }
        }
        awPostRemote([awRemoteEvent(msg)], sync: false)   // live mirror to the server
        DispatchQueue.main.async { self.renderDebug() }
    }

    private func renderDebug() {
        debugTextView.text = debugLines.joined(separator: "\n")
        let end = NSRange(location: debugTextView.text.count, length: 0)
        debugTextView.scrollRangeToVisible(end)
    }

    /// Reload the log written by the previous run (which may have crashed) and
    /// carry it forward, capped to the last 200 lines so the file stays bounded.
    private func loadPreviousLog() {
        let prev = (try? String(contentsOf: awDebugLogURL, encoding: .utf8)) ?? ""
        var carried = prev.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        if carried.count > 200 { carried = Array(carried.suffix(200)) }
        if !carried.filter({ !$0.isEmpty }).isEmpty {
            debugLines.append("──────── previous session ────────")
            debugLines.append(contentsOf: carried)
            debugLines.append("──────── new session ────────")
            // Replay last run's trail to the server too — covers a hard crash
            // (signal) that couldn't post from inside its handler.
            awPostRemote([awRemoteEvent("previous_session_replay", level: "warn",
                                        data: ["log": carried.suffix(120).joined(separator: "\n")])], sync: false)
        }
        // Rewrite the file fresh with the carried-over trail so growth is bounded.
        try? debugLines.joined(separator: "\n").appending("\n").write(to: awDebugLogURL, atomically: true, encoding: .utf8)
    }

    @objc private func copyLogTapped() {
        UIPasteboard.general.string = debugLines.joined(separator: "\n")
        setStatus("📋 Log copied — paste it to share.", color: .systemGreen)
    }

    @objc private func clearLogTapped() {
        debugLines.removeAll()
        try? "".write(to: awDebugLogURL, atomically: true, encoding: .utf8)
        renderDebug()
        setStatus("Log cleared.", color: .secondaryLabel)
    }

    // MARK: - Lifecycle

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground

        // Point remote logging at the saved sync host (falls back to dev.tekflox.com).
        let savedHost = UserDefaults.standard.string(forKey: storageKey) ?? defaultHost
        awRemoteLogBase = originUrl(savedHost)

        awInstallCrashHandlers()       // capture hard crashes (file + remote flush)
        loadPreviousLog()              // surface last run's trail (incl. any crash)
        debugLog("viewDidLoad — iOS \(UIDevice.current.systemVersion) \(UIDevice.current.model)")

        hostField.delegate         = self
        domainFilterField.delegate = self

        let saved = UserDefaults.standard.string(forKey: storageKey) ?? defaultHost
        hostField.text = saved
        debugLog("host: \(saved)")

        updateHint()
        debugLog("setupLayout…")
        setupLayout()
        debugLog("layout OK ✓")
    }

    override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        debugLog("app running ✓")
    }

    // MARK: - Layout

    private func setupLayout() {
        scrollView.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(scrollView)
        NSLayoutConstraint.activate([
            scrollView.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            scrollView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            scrollView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            scrollView.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
        scrollView.addSubview(stackView)
        NSLayoutConstraint.activate([
            stackView.topAnchor.constraint(equalTo: scrollView.topAnchor, constant: 32),
            stackView.leadingAnchor.constraint(equalTo: scrollView.leadingAnchor, constant: 24),
            stackView.trailingAnchor.constraint(equalTo: scrollView.trailingAnchor, constant: -24),
            stackView.bottomAnchor.constraint(equalTo: scrollView.bottomAnchor, constant: -32),
            stackView.widthAnchor.constraint(equalTo: scrollView.widthAnchor, constant: -48),
        ])

        let hostLabel   = sectionLabel("AW Host")
        let filterLabel = sectionLabel("Domain Filter (optional)")

        debugTextView.heightAnchor.constraint(equalToConstant: 180).isActive = true

        // DEBUG LOG header row: label on the left, Copy/Clear buttons on the right.
        let debugHeaderRow = UIStackView(arrangedSubviews: [debugHeaderLabel, UIView(), copyLogButton, clearLogButton])
        debugHeaderRow.axis      = .horizontal
        debugHeaderRow.alignment = .center
        debugHeaderRow.spacing   = 8

        [titleLabel, subtitleLabel,
         hostLabel, hostField, hostHintLabel,
         filterLabel, domainFilterField,
         syncButton, settingsButton,
         statusLabel, extensionStatusLabel,
         debugHeaderRow, debugTextView,
        ].forEach { stackView.addArrangedSubview($0) }

        stackView.setCustomSpacing(4,  after: hostLabel)
        stackView.setCustomSpacing(4,  after: hostField)
        stackView.setCustomSpacing(4,  after: filterLabel)
        stackView.setCustomSpacing(24, after: domainFilterField)
        stackView.setCustomSpacing(8,  after: syncButton)
        stackView.setCustomSpacing(24, after: settingsButton)
        stackView.setCustomSpacing(4,  after: debugHeaderRow)

        let tap = UITapGestureRecognizer(target: self, action: #selector(dismissKeyboard))
        tap.cancelsTouchesInView = false
        view.addGestureRecognizer(tap)
    }

    private func sectionLabel(_ text: String) -> UILabel {
        let l = UILabel()
        l.text      = text.uppercased()
        l.font      = .systemFont(ofSize: 11, weight: .semibold)
        l.textColor = .secondaryLabel
        return l
    }

    // MARK: - Helpers

    private var currentHost: String {
        let t = (hostField.text ?? "").trimmingCharacters(in: .whitespaces)
        return t.isEmpty ? defaultHost : t
    }

    private func syncUrl(_ host: String) -> String {
        let h = host.trimmingCharacters(in: .whitespaces)
        if h.hasPrefix("http://") || h.hasPrefix("https://") {
            return h.hasSuffix("/sync-cookies") ? h : "\(h)/sync-cookies"
        }
        let isLocal = h.hasPrefix("localhost") || h.hasPrefix("127.")
        return "\(isLocal ? "http" : "https")://\(h)/sync-cookies"
    }

    private func originUrl(_ host: String) -> String {
        let h = host.trimmingCharacters(in: .whitespaces)
        if h.hasPrefix("http://") || h.hasPrefix("https://") { return h }
        let isLocal = h.hasPrefix("localhost") || h.hasPrefix("127.")
        return "\(isLocal ? "http" : "https")://\(h)"
    }

    private func updateHint() {
        hostHintLabel.text = "→ \(syncUrl(currentHost))"
    }

    private func setStatus(_ msg: String, color: UIColor = .label) {
        DispatchQueue.main.async {
            self.statusLabel.text      = msg
            self.statusLabel.textColor = color
        }
    }

    // MARK: - Actions

    @objc private func dismissKeyboard() { view.endEditing(true) }

    @objc private func openSettings() {
        if let url = URL(string: "App-Prefs:root=SAFARI&path=WEB_EXTENSIONS") {
            UIApplication.shared.open(url)
        } else {
            UIApplication.shared.open(URL(string: UIApplication.openSettingsURLString)!)
        }
    }

    @objc private func syncTapped() {
        view.endEditing(true)
        let host   = currentHost
        let filter = (domainFilterField.text ?? "").trimmingCharacters(in: .whitespaces)
        UserDefaults.standard.set(host, forKey: storageKey)
        syncButton.isEnabled = false
        setStatus("Reading cookies from Safari…", color: .secondaryLabel)
        debugLog("sync → host=\(host) filter='\(filter)'")

        let cookieStore = WKWebsiteDataStore.default().httpCookieStore
        debugLog("getAllCookies…")
        cookieStore.getAllCookies { [weak self] cookies in
            guard let self else { return }
            self.debugLog("got \(cookies.count) cookies total")

            let filtered: [HTTPCookie] = filter.isEmpty ? cookies : cookies.filter {
                $0.domain.contains(filter) ||
                filter.contains($0.domain.trimmingCharacters(in: CharacterSet(charactersIn: ".")))
            }
            self.debugLog("filtered: \(filtered.count)")

            let hostDomain = host.components(separatedBy: ":").first ?? host
            // aw_id_jwt — the F2 identity system's apex cookie (aw-workspace's
            // IdentityGuard, what actually gates /sync-cookies) is a different
            // cookie from the legacy "aw_jwt" this used to read (auth_utils.py's
            // older, now-bypassed cookie name). Fixed 2026-08-02.
            let awJwt = cookies.first {
                $0.name == "aw_id_jwt" &&
                $0.domain.contains(hostDomain.components(separatedBy: ".").suffix(2).joined(separator: "."))
            }?.value

            guard let token = awJwt else {
                self.debugLog("❌ no aw_id_jwt for \(host)")
                self.setStatus(
                    "⚠️ No aw_id_jwt cookie found for \(host).\nOpen \(self.originUrl(host)) in Safari and log in first.",
                    color: .systemOrange
                )
                DispatchQueue.main.async { self.syncButton.isEnabled = true }
                return
            }
            self.debugLog("aw_id_jwt ✓ — posting \(filtered.count) cookies")
            self.setStatus("Found \(filtered.count) cookies. Posting…", color: .secondaryLabel)

            let cookieList: [[String: Any]] = filtered.map { c in
                var d: [String: Any] = [
                    "name":     c.name,
                    "value":    c.value,
                    "domain":   c.domain,
                    "path":     c.path,
                    "secure":   c.isSecure,
                    "httpOnly": c.isHTTPOnly,
                    "sameSite": {
                        switch c.sameSitePolicy {
                        case .sameSiteLax:    return "Lax"
                        case .sameSiteStrict: return "Strict"
                        default:              return "None"
                        }
                    }(),
                ]
                if let exp = c.expiresDate { d["expirationDate"] = exp.timeIntervalSince1970 }
                return d
            }

            guard let body = try? JSONSerialization.data(withJSONObject: ["cookies": cookieList]),
                  let url  = URL(string: self.syncUrl(host)) else {
                self.debugLog("❌ serialization error")
                self.setStatus("❌ Serialization error.", color: .systemRed)
                DispatchQueue.main.async { self.syncButton.isEnabled = true }
                return
            }
            self.debugLog("POST → \(url)")

            var req = URLRequest(url: url, timeoutInterval: 15)
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            // aw-workspace's IdentityGuard only recognizes Authorization: Bearer
            // or the apex aw_id_jwt cookie — the old X-AW-JWT header was the
            // bespoke monolith proxy_server.py's own auth check, retired
            // 2026-08-02 when /sync-cookies moved onto the app framework's
            // shared identity layer.
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            req.httpBody = body

            URLSession.shared.dataTask(with: req) { [weak self] data, resp, err in
                guard let self else { return }
                DispatchQueue.main.async { self.syncButton.isEnabled = true }
                if let err {
                    self.debugLog("❌ \(err.localizedDescription)")
                    self.setStatus("❌ \(err.localizedDescription)", color: .systemRed)
                    return
                }
                let http = resp as? HTTPURLResponse
                self.debugLog("HTTP \(http?.statusCode ?? 0)")
                if http?.statusCode == 401 {
                    self.setStatus("❌ 401 — log in to \(host) first.", color: .systemRed)
                    return
                }
                if let data, let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    let injected = result["injected"] as? Int ?? 0
                    let failed   = result["failed"]   as? Int ?? 0
                    self.debugLog("✅ injected=\(injected) failed=\(failed)")
                    self.setStatus(
                        "✅ Done: \(filtered.count) read, \(injected) synced\(failed > 0 ? ", \(failed) failed" : "").",
                        color: .systemGreen
                    )
                } else {
                    self.debugLog("✅ HTTP \(http?.statusCode ?? 0)")
                    self.setStatus("✅ Posted \(filtered.count) cookies (HTTP \(http?.statusCode ?? 0)).", color: .systemGreen)
                }
            }.resume()
        }
    }
}

extension ViewController: UITextFieldDelegate {
    func textFieldShouldReturn(_ textField: UITextField) -> Bool {
        textField.resignFirstResponder()
        if textField == hostField {
            UserDefaults.standard.set(currentHost, forKey: storageKey)
            updateHint()
        }
        return true
    }
    func textField(_ textField: UITextField, shouldChangeCharactersIn range: NSRange, replacementString string: String) -> Bool {
        if textField == hostField { DispatchQueue.main.async { self.updateHint() } }
        return true
    }
}

// ──────────────────────────────────────────────────────────────────────────────
#elseif os(macOS)
import Cocoa
import SafariServices

class ViewController: NSViewController {

    private let titleLabel: NSTextField = {
        let l = NSTextField(labelWithString: "AW Cookie Sync")
        l.font             = .boldSystemFont(ofSize: 18)
        l.alignment        = .center
        l.translatesAutoresizingMaskIntoConstraints = false
        return l
    }()

    private let bodyLabel: NSTextField = {
        let l = NSTextField(wrappingLabelWithString:
            "This app provides a Safari extension that syncs your Safari cookies to an " +
            "Agentic Workspace container.\n\n" +
            "To use it, enable the extension below and then click the AW icon in your Safari toolbar."
        )
        l.font      = .systemFont(ofSize: 13)
        l.textColor = .secondaryLabelColor
        l.alignment = .center
        l.translatesAutoresizingMaskIntoConstraints = false
        return l
    }()

    private let openButton: NSButton = {
        let b = NSButton(title: "Open Safari Extension Preferences", target: nil, action: #selector(openExtensionPrefs))
        b.bezelStyle    = .rounded
        b.keyEquivalent = "\r"
        b.translatesAutoresizingMaskIntoConstraints = false
        return b
    }()

    private let statusLabel: NSTextField = {
        let l = NSTextField(labelWithString: "")
        l.font      = .systemFont(ofSize: 12)
        l.textColor = .secondaryLabelColor
        l.alignment = .center
        l.translatesAutoresizingMaskIntoConstraints = false
        return l
    }()

    override func loadView() {
        view = NSView()
        view.setFrameSize(NSSize(width: 380, height: 240))
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        openButton.target = self
        [titleLabel, bodyLabel, openButton, statusLabel].forEach { view.addSubview($0) }
        NSLayoutConstraint.activate([
            titleLabel.topAnchor.constraint(equalTo: view.topAnchor, constant: 28),
            titleLabel.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            bodyLabel.topAnchor.constraint(equalTo: titleLabel.bottomAnchor, constant: 12),
            bodyLabel.leadingAnchor.constraint(equalTo: view.leadingAnchor,   constant: 24),
            bodyLabel.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -24),
            openButton.topAnchor.constraint(equalTo: bodyLabel.bottomAnchor, constant: 20),
            openButton.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            statusLabel.topAnchor.constraint(equalTo: openButton.bottomAnchor, constant: 12),
            statusLabel.centerXAnchor.constraint(equalTo: view.centerXAnchor),
        ])
    }

    override func viewWillAppear() {
        super.viewWillAppear()
        statusLabel.stringValue = "Open Safari → Settings → Extensions to enable the extension."
        statusLabel.textColor   = .secondaryLabelColor
    }

    @objc private func openExtensionPrefs() {
        SFSafariApplication.showPreferencesForExtension(
            withIdentifier: (Bundle.main.bundleIdentifier ?? "") + ".extension"
        ) { _ in }
    }
}
#endif
