# Acurast Processor Bridge

FARMOS now implements the signed **Acurast Processor Management Backend** check-in surface for Android processors.

It is a control-plane bridge, not a replacement processor runtime:

- The official **Acurast Processor Lite/Core** app creates/controls the wallet, performs attestation, executes deployments, and receives rewards.
- FARMOS verifies the Acurast processor's P-256 signature, records check-ins, and can mirror the check-in to a linked FARMOS device.
- FARMOS does not store an Acurast seed phrase, private key, or wallet credential.

## Before onboarding

Use a public HTTPS FARMOS endpoint. Do not use HTTP and do not disable hostname verification.

For a dedicated phone, Acurast Core requires a factory reset and makes the phone an Acurast-only device. For a phone that must keep other apps, use Acurast Processor Lite if the device meets Acurast's current compatibility requirements.

## Connect a processor

1. Onboard the device with the official Acurast Processor app and Acurast Hub.
2. Copy the processor's Acurast address from the Hub.
3. With an owner/admin FARMOS session, pre-register that address **before** enabling the management endpoint:

   ```http
   POST /api/v1/acurast/processors
   Authorization: Bearer <owner-or-admin-session-token>
   Content-Type: application/json

   {
     "address": "<acurast-processor-address>",
     "label": "Stratus C7",
     "farmos_device_id": "<optional-existing-farmos-device-uuid>"
   }
   ```

   Pre-registration is deliberate: signed check-ins from unknown processors receive `403`; a valid signature alone cannot create a fleet record.

4. In **Acurast Hub → Phones → Advanced**, enable the management endpoint and set its base URL to the FARMOS HTTPS origin. Acurast posts to:

   ```text
   https://<farmos-host>/processor/check-in
   ```

5. Verify the first official check-in:

   ```text
   GET https://<farmos-host>/processor/api/<acurast-processor-address>/status
   ```

   A successful result contains `processorStatus` with the processor address, timestamp, battery, charging state, and network type.

## Security contract

- Endpoint: `POST /processor/check-in`
- Required header: `X-Device-Signature`
- Supported sender: Acurast Android processor (`platform: 0`)
- The server recreates the official compact `JSON.stringify` request representation, verifies the P-256 signature, recovers the signer key, derives the Acurast SS58 address, and compares it to the claimed processor address.
- Signatures are accepted only for enabled, pre-registered processor addresses.
- The response includes `refreshIntervalInSeconds`; default is 1800 seconds and is configured with `ACURAST_CHECKIN_REFRESH_SECONDS`.

## Earning status

The bridge is not proof of earnings. Real income requires an official Acurast processor that is onboarded, attested, online, and receiving Acurast network rewards or deployments. Verify rewards in the Acurast Hub/wallet before recording any revenue.
