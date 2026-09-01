# FARMOS — Distributed Edge Revenue Operating System

## Phase 1 Foundation (Current)

### What's implemented
- Repository scaffold: `backend/`, `web/`, `android-node/`, `sdk/`, `docs/`, `infra/`, `scripts/`
- Docker Compose: PostgreSQL 16, Redis 7, backend, workers, web, Mailhog
- FastAPI app with structured JSON logging, request ID middleware, rate limiting, CORS
- SQLAlchemy 2.0 async models + Alembic migrations
- Phase 1 DB schema: users, roles, providers, farms, devices, heartbeats, benchmarks, capabilities, health events, audit logs, system settings, jobs, opportunities, job events, ledger accounts/entries, withdrawals, provider balances
- Auth endpoints: register, login, refresh, logout, /me (JWT + bcrypt)
- RBAC middleware: OWNER, ADMIN, FINANCE, OPERATIONS, DEVELOPER, CUSTOMER, PROVIDER, SUPPORT, NODE
- Device endpoints: enrollment token, enroll, heartbeat, status
- Wallet endpoints: connect-request, verify-signature, list
- Admin endpoints: fleet, users, quarantine/release, revenue summary, risk events
- Provider dashboard: devices + membership
- Customer + withdrawal endpoints (shell)
- Seed script: creates owner/admin with all roles, default farm, system settings
- Worker stubs: scheduler, verification, reconciliation

### How to run
1. `cp .env.example .env`
2. `docker compose up`
3. Backend runs at `http://localhost:8000`
4. API docs at `http://localhost:8000/docs`

### First test
```
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"owner@farmos.local","password":"ChangeMe123!","full_name":"Owner"}'
```

### Next
- Node simulator (Phase 1 item 9)
- First Alembic migration validation
- Unit/integration tests
- Admin dashboard shell
