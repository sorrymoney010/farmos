# Sprint 11 — Android secure node identity

This module implements the first real-phone path for FARMOS:

1. Generate a Keystore-backed EC P-256 device keypair.
2. Request a one-time enrollment token from the FARMOS backend.
3. Submit enrollment signed by the device private key.
4. Store `device_id`, `human_id`, and `node_token` locally.
5. Send signed heartbeats using the node token and device key.
6. Transition the node to `ACTIVE` after the first valid signed heartbeat.

## Current limitations
- No Android SDK / adb is installed on this Mac, so an APK was not built here.
- No physical Android phone is connected to this session, so a real-device enrollment could not be executed yet.
- `FARMOS_ANDROID_ENROLLMENT_BEARER` is required at runtime for controlled Sprint 11 lab enrollment.

## Key files
- `app/src/main/java/com/farmos/node/identity/AndroidKeyStoreManager.kt`
- `app/src/main/java/com/farmos/node/identity/DeviceIdentityStore.kt`
- `app/src/main/java/com/farmos/node/node/NodeEnrollmentRepository.kt`
- `app/src/main/java/com/farmos/node/node/NodeHeartbeatWorker.kt`
- `app/src/main/java/com/farmos/node/MainActivity.kt`

## Backend contract added in Sprint 11
- `POST /api/v1/devices/enrollment-token`
- `POST /api/v1/devices/enroll`
- `POST /api/v1/devices/{device_id}/heartbeat`

Enrollment and heartbeat bodies include:
- `request_nonce`
- `request_timestamp`
- `signature`

The backend verifies:
- one-time enrollment token
- request timestamp freshness
- nonce replay protection in Redis
- ECDSA signature against the enrolled device public key
- node token/device ID match for heartbeats

## Real phone checklist
1. Install Android Studio + SDK 34.
2. Build and install the app on one Android 12+ phone.
3. Export a short-lived `FARMOS_ANDROID_ENROLLMENT_BEARER` for lab enrollment.
4. Tap **Enroll device**.
5. Confirm the device appears in FARMOS admin fleet with status `ACTIVE`.
