# Wireless (no-USB) device onboarding

FARMOS devices enroll and run jobs over the network. USB/ADB is only needed to install
the APK the first time (or use wireless ADB / LAN sideload). After install, claim codes
replace long enrollment tokens, and the Android node polls/`http_check` jobs on Wi‑Fi.

## Operator flow (end-to-end)

1. **Install the APK wirelessly** (once):
   - Build: `cd android-node && ./gradlew :app:assembleDebug`
   - Wireless ADB:
     ```bash
     # Phone: enable Wireless debugging (or once via USB: adb tcpip 5555)
     ./scripts/wireless_apk_install.sh 192.168.1.42
     ```
   - Or serve over LAN for browser sideload:
     ```bash
     ./scripts/wireless_apk_install.sh --serve
     # On phone: open http://<lan-ip>:8765/app-debug.apk and install
     ```
2. **Mint a claim code** (owner/admin/provider), or use staging `123`:
   ```bash
   curl -X POST "$API/api/v1/devices/claim-code" \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"expires_in_minutes": 15}'
   ```
3. **On the phone** (FARMOS Node app): set API base URL → enter claim code (or `123`) → Enroll.
   The app exchanges the code, signs enrollment with Android Keystore, then starts
   **heartbeats + job loop** automatically.
4. **Dispatch an `http_check` job** to the device (dashboard or API). The phone will:
   poll → accept (signed) → HTTP GET/POST → submit signed result → earn test credit.
5. **Python agent / simulator** (optional):
   ```bash
   export FARMOS_API_URL=https://api.example.com
   export FARMOS_CLAIM_CODE=A7K9MP   # or 123 on staging
   python -m app.agent.node_agent
   ```

## Staging fixed code `123`

When **`STAGING_CLAIM_CODES=true`** (explicit env flag; never enable in production):

- Claim code **`123`** is accepted without minting.
- Each exchange issues a fresh one-time `enroll-…` enrollment token for
  `STAGING_ADMIN_USER_ID`.
- Code `123` is reusable in staging for lab convenience; the enrollment token it
  returns is still one-time and signature-checked before consume.

When `STAGING_CLAIM_CODES=false` (default / production), `123` is rejected like any
other unknown claim code. Production only uses random short codes from
`POST /devices/claim-code`.

## Security notes

- Claim codes map to the same owner/farm payload as enrollment tokens (Redis).
- Devices still call `POST /devices/enroll` with a device signature.
- Signature verification runs **before** the one-time enrollment token is consumed.
- Job accept/result signatures use the enrolled device public key (same Keystore key
  as heartbeats). Result signatures cover `request_nonce`, `request_timestamp`,
  `job_id`, `device_id`, `result_hash`, `result_uri` only.
- Prefer claim-code exchange over inventing a parallel insecure enroll path.

## Staging mint (optional)

```bash
curl -X POST "$API/api/v1/devices/staging/claim-code" \
  -H "X-Staging-Enroll-Admin: $STAGING_ENROLL_ADMIN_SECRET" \
  -H "Content-Type: application/json" -d '{}'
```

When staging claim codes are enabled, the response also reminds operators that
`123` works as the fixed convenience code.
