"""FARMOS API entrypoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.logging import setup_logging, get_logger
from app.middleware.request_id import RequestIDMiddleware
from app.middleware.audit import AuditMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.db.session import init_db, close_db
from app.security.node_auth import init_redis, close_redis
from app.api import api_router
from app.api.acurast import management_router

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info("farmos_starting", app_env=settings.APP_ENV)
    await init_db()
    init_redis()
    yield
    await close_db()
    await close_redis()
    logger.info("farmos_shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="FARMOS API",
        description="Distributed Edge Revenue Operating System",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(AuditMiddleware)
    app.add_middleware(
        RateLimitMiddleware,
        requests=settings.RATE_LIMIT_REQUESTS,
        window=settings.RATE_LIMIT_WINDOW_SECONDS,
    )
    app.include_router(api_router)
    # The official Acurast Processor uses these root paths for a custom
    # management endpoint; keep them outside FARMOS's versioned user API.
    app.include_router(management_router, tags=["acurast-management"])

    @app.get("/health")
    async def health():
        return {
            "status": "healthy",
            "app_env": settings.APP_ENV,
            "timestamp": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        }

    @app.get("/admin/enroll")
    async def admin_enroll_page():
        from fastapi.responses import HTMLResponse
        html = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FARMOS Remote Enrollment</title>
<script src="https://cdn.jsdelivr.net/npm/qrcode@1.5.3/build/qrcode.min.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
body { background: #f5f5f5; padding: 20px; }
.container { max-width: 480px; margin: 0 auto; background: white; border-radius: 12px; padding: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
h1 { font-size: 20px; margin-bottom: 16px; color: #1a1a1a; }
label { display: block; font-size: 14px; font-weight: 500; margin-bottom: 6px; color: #444; }
input, select { width: 100%; padding: 10px 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 14px; margin-bottom: 16px; }
button { width: 100%; padding: 12px; background: #2563eb; color: white; border: none; border-radius: 8px; font-size: 15px; font-weight: 600; cursor: pointer; }
button:hover { background: #1d4ed8; }
button:disabled { background: #9ca3af; cursor: not-allowed; }
.result { margin-top: 20px; padding: 16px; background: #f9fafb; border-radius: 8px; display: none; }
.result.active { display: block; }
.qr { text-align: center; margin: 16px 0; }
.qr canvas, .qr img { border: 1px solid #e5e7eb; border-radius: 8px; }
.link { word-break: break-all; padding: 10px; background: white; border: 1px solid #e5e7eb; border-radius: 6px; font-size: 13px; color: #2563eb; text-decoration: none; display: block; margin-top: 8px; }
.error { color: #dc2626; font-size: 13px; margin-top: 8px; }
</style>
</head>
<body>
<div class="container">
<div id="authSection">
<h1>FARMOS Remote Device Enrollment</h1>
<p style="font-size:13px;color:#666;margin-bottom:12px;">Enter the staging admin secret to continue.</p>
<label for="secret">Staging Admin Secret</label>
<input type="password" id="secret" placeholder="Paste staging admin secret">
<button onclick="doLogin()">Continue</button>
<div id="authError" class="error"></div>
</div>
<div id="enrollSection" style="display:none;">
<h1>FARMOS Remote Device Enrollment</h1>
<label for="farm">Farm / Provider</label>
<input type="text" id="farm" placeholder="e.g., farm-001">
<label for="expires">Expires (minutes)</label>
<input type="number" id="expires" value="15" min="5" max="60">
<button onclick="issueToken()">Generate Enrollment Link</button>
<div id="result" class="result">
<p><strong>Enrollment Link</strong></p>
<a id="enrollLink" class="link" href="#" target="_blank"></a>
<div class="qr" id="qr"></div>
<p style="font-size: 12px; color: #666;">Scan with FARMOS Android app or open link on device. Link expires in <span id="expiry"></span>.</p>
</div>
<div id="error" class="error"></div>
</div>
</div>
<script>
let stagingSecret = localStorage.getItem('farmos_staging_secret') || '';

function saveSecret(secret) {
  stagingSecret = secret;
  localStorage.setItem('farmos_staging_secret', secret);
}

function authHeaders() {
  const headers = { 'Content-Type': 'application/json', 'Accept': 'application/json' };
  if (stagingSecret) headers['X-Staging-Enroll-Admin'] = stagingSecret;
  return headers;
}

function showEnroll() {
  document.getElementById('authSection').style.display = 'none';
  document.getElementById('enrollSection').style.display = 'block';
}

function showAuthSection() {
  document.getElementById('authSection').style.display = 'block';
  document.getElementById('enrollSection').style.display = 'none';
}

async function doLogin() {
  const secret = document.getElementById('secret').value.trim();
  const error = document.getElementById('authError');
  error.textContent = '';
  if (!secret) { error.textContent = 'Secret is required'; return; }
  try {
    const res = await fetch('/api/v1/devices/staging/enrollment-token', {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ farm_id: 'staging', expires_in_minutes: 1 })
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || `HTTP ${res.status}`);
    }
    saveSecret(secret);
    showEnroll();
  } catch (e) {
    error.textContent = e.message || 'Authentication failed';
  }
}

async function issueToken() {
  const error = document.getElementById('error');
  const result = document.getElementById('result');
  error.textContent = '';
  result.classList.remove('active');
  const farm = document.getElementById('farm').value.trim();
  const expires = parseInt(document.getElementById('expires').value, 10) || 15;
  try {
    const res = await fetch('/api/v1/devices/enrollment-token', {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ farm_id: farm || undefined, expires_in_minutes: expires })
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    const link = data.qr_payload || `${window.location.origin}/enroll?token=${data.enrollment_token}`;
    document.getElementById('enrollLink').href = link;
    document.getElementById('enrollLink').textContent = link;
    document.getElementById('expiry').textContent = data.expires_at ? new Date(data.expires_at).toLocaleString() : `${expires} minutes`;
    document.getElementById('qr').innerHTML = '';
    QRCode.toCanvas(document.createElement('canvas'), link, { width: 220, margin: 2 }, function (error, canvas) {
      if (error) { console.error(error); return; }
      document.getElementById('qr').appendChild(canvas);
    });
    result.classList.add('active');
  } catch (e) {
    error.textContent = e.message || 'Failed to issue enrollment token';
  }
}

if (stagingSecret) showEnroll();
</script>
</body>
</html>"""
        return HTMLResponse(content=html)

    @app.get("/metrics")
    async def metrics():
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST  # noqa: PLC0415
        from fastapi.responses import Response  # noqa: PLC0415
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
