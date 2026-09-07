#!/bin/bash
set -euo pipefail
cd /Users/musicmancheef/farmos

SECRET=$(grep '^STAGING_ENROLL_ADMIN_SECRET=' .env | cut -d= -f2)
if [ -z "$SECRET" ]; then
  echo "ERROR: STAGING_ENROLL_ADMIN_SECRET not found in .env"
  exit 1
fi

# farm_id is the textual farm/provider identifier the staging token is associated with.
# Pass a test identifier here; the backend accepts a free-text String(100).
FARM_ID="farmos-staging-test-farm-001"

echo "Issuing enrollment token via staging endpoint..."
RESPONSE=$(curl -s -X POST \
  "https://sticky-graphical-interim-modules.trycloudflare.com/api/v1/devices/staging/enrollment-token" \
  -H "Content-Type: application/json" \
  -H "X-Staging-Enroll-Admin: $SECRET" \
  -d "{\"farm_id\": \"$FARM_ID\", \"expires_in_minutes\": 15}")

echo "Raw response:"
echo "$RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$RESPONSE"
echo ""
echo "Extracting token and link..."
TOKEN=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('enrollment_token',''))" 2>/dev/null)
LINK=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('qr_payload',''))" 2>/dev/null)

echo "TOKEN: $TOKEN"
echo "LINK:  $LINK"

if [ -z "$TOKEN" ]; then
  echo "ERROR: failed to get enrollment token"
  exit 1
fi

# Save to temp file for use on device
echo "$TOKEN" > /tmp/farmos_enroll_token.txt
echo "$LINK" > /tmp/farmos_enroll_link.txt
echo "Saved to /tmp/farmos_enroll_token.txt and /tmp/farmos_enroll_link.txt"
