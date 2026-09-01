"""Customer endpoints for FARMOS."""

from fastapi import APIRouter, Depends

router = APIRouter()


@router.get("/balance")
async def get_balance():
    return {"balance_usd": 0.0, "currency": "USD"}


@router.post("/jobs")
async def create_customer_job():
    return {"detail": "Customer jobs pending marketplace implementation"}
