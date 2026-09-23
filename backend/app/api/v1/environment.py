"""环境监测路由：GET /api/environment/summary、GET /api/environment/detail。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.response import ok
from app.db.session import get_db
from app.services import get_environment_service

router = APIRouter(prefix="/environment", tags=["environment"])


@router.get("/summary")
def summary(db: Session = Depends(get_db), user_id: str = Depends(get_current_user)):
    return ok(get_environment_service().summary(db, user_id))


@router.get("/detail")
def detail(db: Session = Depends(get_db), user_id: str = Depends(get_current_user)):
    return ok(get_environment_service().detail(db, user_id))