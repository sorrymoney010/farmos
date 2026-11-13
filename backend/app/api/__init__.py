"""API v1 router aggregation."""

from fastapi import APIRouter

from app.api import (acurast, admin, auth, customer, developer, devices, finance_admin, jobs, opportunities,
                     provider, revenue_admin, stripe_webhooks, wallets, withdrawals)

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(wallets.router, prefix="/wallets", tags=["wallets"])
api_router.include_router(devices.router, prefix="/devices", tags=["devices"])
api_router.include_router(acurast.admin_router, prefix="/acurast", tags=["acurast"])
api_router.include_router(jobs.router, prefix="/jobs", tags=["jobs"])
api_router.include_router(provider.router, prefix="/provider", tags=["provider"])
api_router.include_router(customer.router, prefix="/customer", tags=["customer"])
api_router.include_router(withdrawals.router, prefix="/withdrawals", tags=["withdrawals"])
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])
api_router.include_router(revenue_admin.router, prefix="/admin", tags=["admin-revenue-engine"])
api_router.include_router(finance_admin.router, prefix="/admin/finance", tags=["admin-finance"])
api_router.include_router(opportunities.router, prefix="/opportunities", tags=["opportunities"])
api_router.include_router(developer.router, prefix="/developer", tags=["developer"])
api_router.include_router(developer.admin_router, prefix="/admin/developer", tags=["admin-developer"])
api_router.include_router(stripe_webhooks.router, prefix="/webhooks", tags=["webhooks"])

__all__ = ["api_router"]
