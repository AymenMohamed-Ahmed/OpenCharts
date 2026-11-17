from fastapi import APIRouter

from app.common.schemas import Health

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> Health:
    return Health(service="api")
