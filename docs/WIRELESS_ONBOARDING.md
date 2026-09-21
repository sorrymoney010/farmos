# Wireless (no-USB) device onboarding

FARMOS devices enroll over the network. USB/ADB is only needed to install the APK
the first time (or use wireless ADB / sideload). Claim codes replace long enrollment
tokens for phone typing.

## Operator flow

1. **Install the APK (once)** — no enrollment cable required after install:
   - Download the APK from your release/download link and open it on the phone, or
   - `adb install -r app-debug.apk` over USB **or** wireless ADB (`adb connect <ip>:5555`), or
   - Sideload over Wi‑Fi via your preferred MDM / file share.
2. **Mint a claim code** (owner/admin/provider):
   ```bash
   curl -X POST "$API/api/v1/devices/claim-code" \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"expires_in_minutes": 15}'
   ```
   Response includes a short `claim_code` (e.g. `A7K9MP`).
3. **On the phone** (FARMOS Node app): set API base URL → enter the claim code → Enroll.
   The app exchanges the code for a one-time enrollment token, then signs enrollment
   with the Android Keystore key (same security model as QR/`enroll-…` tokens).
4. **Python agent / simulator**:
   ```bash
   export FARMOS_API_URL=https://api.example.com
   export FARMOS_CLAIM_CODE=A7K9MP
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
- Prefer claim-code exchange over inventing a parallel insecure enroll path.

## Staging mint (optional)

```bash
curl -X POST "$API/api/v1/devices/staging/claim-code" \
  -H "X-Staging-Enroll-Admin: $STAGING_ENROLL_ADMIN_SECRET" \
  -H "Content-Type: application/json" -d '{}'
```

When staging claim codes are enabled, the response also reminds operators that
`123` works as the fixed convenience code.