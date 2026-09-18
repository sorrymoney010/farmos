#!/bin/bash
# FARMOS Acurast Deploy Script
# Uploads the service to IPFS and deploys to Acurast

set -e

SERVICE_DIR="/Users/musicmancheef/farmos/services/farmos-acurast-compute"
cd "$SERVICE_DIR"

echo "=== Bundling service ==="
zip -r farmos-acurast-compute.zip server.js acurast.json

echo "=== Uploading to IPFS ==="
# Option 1: Use Acurast CLI with IPFS
# npx @acurast/cli deploy --only-upload

echo "=== Alternative: Use Acurast Hub ==="
echo "1. Open https://hub.acurast.com/"
echo "2. Click 'Create Deployments'"
echo "3. Upload farmos-acurast-compute.zip or paste IPFS URL"
echo "4. Set execution schedule and reward"
echo "5. Select your Stratus C7 processor (optional)"
echo "6. Click 'Publish Deployment'"

echo ""
echo "=== Service ready for deployment ==="
echo "Files:"
echo "  - server.js (Node.js HTTP service)"
echo "  - acurast.json (deployment config)"
echo "  - farmos-acurast-compute.zip (bundle)"
