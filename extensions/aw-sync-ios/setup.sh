#!/usr/bin/env bash
# setup.sh — Generate the Xcode project for AW Sync Safari Extension.
#
# Run this on a Mac with Xcode 14+ installed:
#   cd tools/browser/aw-sync-extension-ios
#   bash setup.sh
#
# After running:
#   1. Open AW\ Sync/AW\ Sync.xcodeproj in Xcode.
#   2. Set your Apple Developer Team in Signing & Capabilities for both targets.
#   3. Build and run on a simulator or device (iOS) or locally (macOS).
#   4. Enable the extension in:
#        iOS    → Settings → Safari → Extensions → AW Sync Extension → Allow
#        macOS  → Safari → Settings → Extensions → ✓ AW Sync Extension

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXTENSION_DIR="$SCRIPT_DIR/extension"
APP_NAME="AW Sync"
BUNDLE_ID="${BUNDLE_ID:-com.tekflox.awmetaglasses}"
PROJECT_DIR="$SCRIPT_DIR/$APP_NAME"

# ── Pre-flight ────────────────────────────────────────────────────────────────

echo "▶ Checking for safari-web-extension-converter…"
if ! xcrun --find safari-web-extension-converter &>/dev/null; then
  echo "✗ safari-web-extension-converter not found."
  echo "  Install Xcode from the App Store, then run:"
  echo "    xcode-select --install"
  exit 1
fi

# ── Copy icons from Chrome extension ─────────────────────────────────────────

CHROME_EXT="$SCRIPT_DIR/../aw-sync-chrome"
IMAGES_DIR="$EXTENSION_DIR/images"

echo "▶ Copying icons from Chrome extension…"
cp -f "$CHROME_EXT/icon16.png" "$IMAGES_DIR/icon-16.png"
cp -f "$CHROME_EXT/icon48.png" "$IMAGES_DIR/icon-48.png"

# icon-128.png: scale icon48 with sips if not already present
if [ ! -f "$IMAGES_DIR/icon-128.png" ]; then
  echo "  Generating icon-128.png from icon48.png via sips…"
  sips -z 128 128 "$IMAGES_DIR/icon-48.png" --out "$IMAGES_DIR/icon-128.png" &>/dev/null || true
fi

# ── Convert ───────────────────────────────────────────────────────────────────

echo "▶ Running safari-web-extension-converter…"

# Remove old generated project if re-running
if [ -d "$PROJECT_DIR" ]; then
  echo "  Removing existing project at $PROJECT_DIR…"
  rm -rf "$PROJECT_DIR"
fi

xcrun safari-web-extension-converter \
  --app-name        "$APP_NAME"  \
  --bundle-identifier "$BUNDLE_ID" \
  --project-location  "$SCRIPT_DIR" \
  --swift \
  --no-prompt \
  "$EXTENSION_DIR"

# ── Apply native overrides ────────────────────────────────────────────────────
# safari-web-extension-converter generates the host app sources in a directory
# whose name varies by Xcode version (e.g. "AW Sync", "AW Sync (iOS)", etc.).
# We use `find` to locate every ViewController.swift that isn't inside the
# extension target, then replace them all with the unified platform file.

echo "▶ Applying custom ViewController files…"
echo "  Generated project structure:"
find "$PROJECT_DIR" -maxdepth 2 -type d | sort

UNIFIED_VC="$SCRIPT_DIR/native/ViewController.swift"
replaced=0

while IFS= read -r vc_path; do
  # Skip the extension target's ViewController (it doesn't have one, but be safe)
  if echo "$vc_path" | grep -q "Extension"; then
    continue
  fi
  cp -f "$UNIFIED_VC" "$vc_path"
  echo "  ✓ Replaced: $vc_path"
  replaced=$((replaced + 1))
done < <(find "$PROJECT_DIR" -name "ViewController.swift" -not -path "*Extension*")

if [ "$replaced" -eq 0 ]; then
  echo "  ⚠ No ViewController.swift found in generated project."
  echo "    Manually copy native/ViewController.swift into the app target(s)."
fi

# ── Patch Info.plist — skip export compliance question ───────────────────────
# ITSAppUsesNonExemptEncryption = NO tells Apple the app uses no non-exempt
# encryption, so TestFlight skips the manual compliance question on every build.

echo "▶ Patching Info.plist (ITSAppUsesNonExemptEncryption = NO)…"
patched=0

while IFS= read -r plist_path; do
  if echo "$plist_path" | grep -q "Extension"; then
    continue
  fi
  /usr/libexec/PlistBuddy -c "Add :ITSAppUsesNonExemptEncryption bool NO" "$plist_path" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Set :ITSAppUsesNonExemptEncryption NO" "$plist_path"
  echo "  ✓ Patched: $plist_path"
  patched=$((patched + 1))
done < <(find "$PROJECT_DIR" -name "Info.plist" -not -path "*Extension*")

if [ "$patched" -eq 0 ]; then
  echo "  ⚠ No app Info.plist found — builds may require manual export compliance answers."
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo "✅  Done!"
echo ""
echo "  Project: $PROJECT_DIR/$APP_NAME.xcodeproj"
echo ""
echo "  Next steps:"
echo "  1. Open the project in Xcode:"
echo "       open \"$PROJECT_DIR/$APP_NAME.xcodeproj\""
echo "  2. Select your Team in Signing & Capabilities for both the app and extension targets."
echo "  3. Build & run."
echo "  4. Enable the extension:"
echo "       iOS:   Settings → Safari → Extensions → AW Sync Extension → Allow"
echo "       macOS: Safari → Settings → Extensions → ✓ AW Sync Extension"
