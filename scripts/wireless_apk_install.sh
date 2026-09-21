#!/usr/bin/env bash
# Install FARMOS Android Node APK over Wi‑Fi (wireless ADB). No USB after TCP mode is on.
#
# Prerequisites (one-time, may need USB once to enable wireless debugging):
#   1. On phone: Developer options → Wireless debugging ON (Android 11+)
#      OR: adb tcpip 5555 while USB-connected, then unplug.
#   2. Phone and workstation on the same LAN.
#   3. adb available on PATH; APK built or path provided.
#
# Usage:
#   ./scripts/wireless_apk_install.sh <phone-ip> [path/to/app-debug.apk]
#   FARMOS_DEVICE_IP=192.168.1.42 ./scripts/wireless_apk_install.sh
#
# Optional: serve the APK over LAN for manual sideload (no adb):
#   ./scripts/wireless_apk_install.sh --serve [path/to/app-debug.apk] [port]

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEFAULT_APK="$ROOT/android-node/app/build/outputs/apk/debug/app-debug.apk"
PORT="${FARMOS_APK_SERVE_PORT:-8765}"

die() { echo "ERROR: $*" >&2; exit 1; }

serve_apk() {
  local apk="${1:-$DEFAULT_APK}"
  local port="${2:-$PORT}"
  [[ -f "$apk" ]] || die "APK not found: $apk (build with: cd android-node && ./gradlew :app:assembleDebug)"
  echo "Serving $(basename "$apk") on http://0.0.0.0:${port}/$(basename "$apk")"
  echo "On the phone browser open: http://<this-machine-lan-ip>:${port}/$(basename "$apk")"
  echo "Then install the downloaded APK (allow unknown sources if prompted)."
  cd "$(dirname "$apk")"
  exec python3 -m http.server "$port"
}

if [[ "${1:-}" == "--serve" ]]; then
  serve_apk "${2:-$DEFAULT_APK}" "${3:-$PORT}"
fi

IP="${1:-${FARMOS_DEVICE_IP:-}}"
APK="${2:-$DEFAULT_APK}"

[[ -n "$IP" ]] || die "Usage: $0 <phone-ip> [apk]   or   $0 --serve [apk] [port]"
[[ -f "$APK" ]] || die "APK not found: $APK (build with: cd android-node && ./gradlew :app:assembleDebug)"

command -v adb >/dev/null || die "adb not found on PATH"

TARGET="${IP}"
if [[ "$TARGET" != *:* ]]; then
  TARGET="${IP}:5555"
fi

echo "Connecting wireless ADB → $TARGET"
adb connect "$TARGET" >/dev/null
adb -s "$TARGET" wait-for-device
echo "Installing $APK …"
adb -s "$TARGET" install -r "$APK"
echo "Launching FARMOS Node…"
adb -s "$TARGET" shell am start -n com.farmos.node/.MainActivity >/dev/null || true
echo "Done. On the phone: set API URL → claim code 123 (staging) or minted code → Enroll."
echo "Jobs poll automatically after enroll + ACTIVE heartbeat."
