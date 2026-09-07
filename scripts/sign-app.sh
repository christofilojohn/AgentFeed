#!/usr/bin/env bash
# Sign, notarise and staple dist/AgentFeed.app so it opens on any Mac without
# the "unidentified developer" block. Run after scripts/build-app.sh.
#
# Needs, once:
#   1. A "Developer ID Application" certificate in your login keychain.
#      (Apple Developer Program membership; Xcode → Settings → Accounts →
#      Manage Certificates → + → Developer ID Application.)
#   2. A notarytool keychain profile with an app-specific password:
#        xcrun notarytool store-credentials agentfeed \
#            --apple-id YOU@EXAMPLE.COM --team-id TEAMID
#      It asks for the password interactively and stores it in the keychain.
#
# Usage:  scripts/sign-app.sh                # finds the Developer ID identity
#         SIGN_ID="Developer ID Application: Name (TEAMID)" scripts/sign-app.sh
#         NOTARY_PROFILE=other scripts/sign-app.sh
set -euo pipefail
cd "$(dirname "$0")/.."

APP=dist/AgentFeed.app
ZIP=dist/AgentFeed-macos-arm64.zip
ENT=packaging/entitlements.plist
NOTARY_PROFILE="${NOTARY_PROFILE:-agentfeed}"

[ -d "$APP" ] || { echo "no $APP — run scripts/build-app.sh first"; exit 1; }

if [ -z "${SIGN_ID:-}" ]; then
  SIGN_ID=$(security find-identity -v -p codesigning \
            | grep -o '"Developer ID Application: [^"]*"' | head -1 | tr -d '"' || true)
fi
if [ -z "$SIGN_ID" ]; then
  echo "No 'Developer ID Application' certificate in the keychain."
  echo "Identities present:"
  security find-identity -v -p codesigning | sed 's/^/   /'
  echo
  echo "An 'Apple Development' certificate signs for your own Macs only; Gatekeeper"
  echo "does not accept it for downloads. Create a Developer ID Application"
  echo "certificate (needs the paid Developer Program), then rerun."
  exit 2
fi
echo "Signing with: $SIGN_ID"

# Inside-out: every Mach-O first (dylibs, .so extension modules, helper
# binaries), then the bundle. --deep alone misses nested frameworks.
find "$APP" -type f \( -name '*.dylib' -o -name '*.so' -o -perm -u+x \) \
  | while read -r f; do
      if file "$f" | grep -q 'Mach-O'; then
        codesign --force --options runtime --timestamp \
                 --entitlements "$ENT" --sign "$SIGN_ID" "$f" 2>/dev/null
      fi
    done
codesign --force --options runtime --timestamp --entitlements "$ENT" \
         --sign "$SIGN_ID" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"
echo "Signed."

echo "Notarising (profile: $NOTARY_PROFILE) …"
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"
xcrun notarytool submit "$ZIP" --keychain-profile "$NOTARY_PROFILE" --wait
xcrun stapler staple "$APP"
spctl --assess --type execute --verbose=2 "$APP"

# The zip must contain the *stapled* app.
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"
echo
echo "Done: $ZIP opens on any Mac with a double-click."
