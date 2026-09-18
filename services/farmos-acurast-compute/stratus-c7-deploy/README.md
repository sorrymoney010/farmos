#!/bin/bash
# FARMOS Acurast Deploy Script
# Uploads the service to IPFS and deploys to Acurast Hub

set -e

SERVICE_DIR="/Users/musicmancheef/farmos/services/farmos-acurast-compute"
cd "$SERVICE_DIR"

echo "=== FARMOS Acurast Service Bundle ==="
echo ""
echo "Files for Acurast Hub import:"
echo "  - server.js (Node.js HTTP service)"
echo "  - acurast.json (deployment config)"
echo "  - farmos-acurast-compute.zip (ready-to-upload bundle)"
echo ""
echo "=== To Deploy ==="
echo "1. Open https://hub.acurast.com/"
echo "2. Click 'Create Deployments'"
echo "3. Upload farmos-acurast-compute.zip"
echo "4. Configure reward and schedule"
echo "5. Select your Stratus C7 processor"
echo "6. Click 'Publish Deployment'"
echo ""
echo "=== Service Testing ==="
echo "Health: curl http://localhost:3000/health"
echo "Compute: curl -X POST http://localhost:3000/compute -H 'Content-Type: application/json' -d '{\"message\":\"test\"}'"
