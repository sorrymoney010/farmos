# Sprint 11 — Android secure node identity

This module implements the first real-phone path for FARMOS: a device proves its
identity with an Android Keystore-backed key, enrolls with a short-lived one-time
token issued by the admin console, and sends signed heartbeats.

## Security model

- **Device key:** EC P-256 keypair generated in Android Keystore (`AndroidKeyStore`).
  Private key is non-exportable. The public key (PEM) is sent at enrollment and used
  to verify every signed request.
- **No admin credentials on device:** the app never holds an admin bearer token. The
  admin console issues a one-time enrollment token (QR / `farmos://enroll?token=…` /
  manual paste). The phone only *consumes* it.
- **Signed requests:** enrollment and every heartbeat are signed over a canonical JSON
  form (sorted keys, no whitespace) with `SHA256withECDSA`. The backend verifies the
  signature **before** consuming the one-time token, so a bad signed request cannot
  burn a valid token.
- **Atomic token consumption:** Redis `GETDEL` — exactly one enrollment succeeds per
  token; reuse returns 400. A concurrency test proves it.
- **Encrypted token at rest:** the node bearer token is encrypted with an Android
  Keystore-backed AES-256-GCM key (`TokenCipher`); never stored in plaintext
  SharedPreferences.
- **Transport:** HTTPS by default. `10.0.2.2` (emulator loopback) cleartext is allowed
  **only** in the `debug`/LAN-lab network-security-config and only via the build flag
  `FARMOS_ALLOW_INSECURE_HTTP=true`. The `release`/main config forbids cleartext
  everywhere. Hostname verification is never disabled.

## Heartbeat ↔ offline policy reconciliation

Backend policy (seeded `system_settings`):
- `node.heartbeat_interval_seconds = 30`
- `node.offline_after_seconds = 180` (3 minutes)

Android design:
- **Primary cadence:** an in-process 30s loop (`HeartbeatLoop`) while the app process
  is alive. This tolerates up to ~5 missed beats before the 3-minute offline mark.
- **Fallback:** a `WorkManager` periodic worker (`NodeHeartbeatWorker`) at the platform
  minimum 15-minute interval, so heartbeats survive process death. WorkManager cannot
  meet a 3-minute policy on its own; the in-process loop is the primary mechanism and
  the 3-minute grace absorbs Doze deferrals.
- On enroll and on boot (if already enrolled), both are scheduled.

## Build & verify

Requires Android SDK + JDK 17. Network-security config is variant-specific:
`app/src/main/res/xml/network_security_config.xml` (release: no cleartext) and
`app/src/debug/...` (LAN-lab cleartext to 10.0.2.2).

```
export ANDROID_HOME=~/Library/Android/sdk
export JAVA_HOME=<jdk17>
./gradlew :app:assembleDebug :app:testDebugUnitTest
```

Unit tests (`app/src/test`) run on JVM via Robolectric and prove: the shared golden
signature fixture verifies under Android's `Signature` API; release-style builds reject
cleartext API URLs; the node token is encrypted at rest.

## Cross-language canonicalization contract

`android-node/signature_fixture.json` is the golden fixture shared with the Python
backend tests (`backend/app/tests/integration/fixtures/signature_fixture.json`). Both
sides must produce the identical canonical JSON byte string for a given payload; the
Android `canonicalize` (JSONObject + `TreeSet` key ordering) and Python
`json.dumps(..., sort_keys=True, separators=(",",":"))` are equivalent.

## Wireless claim codes (no USB)

Enrollment itself is already network-based. USB is only for APK install (or use
wireless ADB / sideload). Prefer short claim codes on the phone:

1. Operator mints `POST /api/v1/devices/claim-code` (or uses staging `123` when
   `STAGING_CLAIM_CODES=true`).
2. On the device, enter the claim code in the token field and tap Enroll.
3. The app calls `POST /api/v1/devices/claim-code/exchange`, then the normal signed
   `/api/v1/devices/enroll` path.

See `docs/WIRELESS_ONBOARDING.md` for the full operator flow and APK-without-USB notes.

## Job loop (Wi‑Fi http_check)

After enroll, `JobLoop` runs alongside `HeartbeatLoop`:

1. `POST /api/v1/jobs/device/jobs/next` (Bearer node token)
2. `POST …/accept` — signed over `request_nonce`, `request_timestamp`, `job_id`, `device_id`
3. Execute `http_check` (OkHttp GET/POST per job payload)
4. `POST …/result` — signed over nonce/ts/job_id/device_id/`result_hash`/`result_uri`
   (`execution_metrics` is sent but **not** part of the signature)

`result_hash` = SHA-256 hex of canonical JSON of the result object (same as Python agent).

Wireless APK install: `./scripts/wireless_apk_install.sh <phone-ip>` — see `docs/WIRELESS_ONBOARDING.md`.
