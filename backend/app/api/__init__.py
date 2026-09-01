"""API v1 router aggregation."""

from fastapi import APIRouter

from app.api import auth, devices, wallets, jobs, provider, customer, withdrawals, admin

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(wallets.router, prefix="/wallets", tags=["wallets"])
api_router.include_router(devices.router, prefix="/devices", tags=["devices"])
api_router.include_router(jobs.router, prefix="/jobs", tags=["jobs"])
api_router.include_router(provider.router, prefix="/provider", tags=["provider"])
api_router.include_router(customer.router, prefix="/customer", tags=["customer"])
api_router.include_router(withdrawals.router, prefix="/withdrawals", tags=["withdrawals"])
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])

__all__ = ["api_router"]
