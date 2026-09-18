#!/bin/bash
# FARMOS Acurast iPhone Deployment Script

set -e

SERVICE_DIR="/Users/musicmancheef/farmos/services/farmos-acurast-compute"
IPHONE_DIR="$SERVICE_DIR/iphone"

echo "=== FARMOS iPhone Acurast Deployment ==="
echo ""
echo "Processor address: 5EgBZuiRnCfFzvBjgHitNymetm9daFoxEry8vn65sE4QErTj"
echo ""
echo "Files for Acurast Hub import:"
echo "  - server.js (Node.js HTTP service)"
echo "  - acurast.json (deployment config)"
echo "  - package.json"
echo "  - iphone-deploy.zip (ready-to-upload bundle)"
echo ""
echo "Bundle location:"
echo "  $SERVICE_DIR/iphone-deploy.zip"
echo ""
echo "=== To Deploy ==="
echo "1. Open https://hub.acurast.com/"
echo "2. Click 'Create Deployments'"
echo "3. Upload iphone-deploy.zip"
echo "4. Configure reward and schedule"
echo "5. Verify processor whitelist: 5EgBZuiRnCfFzvBjgHitNymetm9daFoxEry8vn65sE4QErTj"
echo "6. Click 'Publish Deployment'"
echo ""
echo "=== Service Testing ==="
echo "Health: curl http://localhost:3000/health"
echo "Compute: curl -X POST http://localhost:3000/compute -H 'Content-Type: application/json' -d '{\"message\":\"test\"}'"
