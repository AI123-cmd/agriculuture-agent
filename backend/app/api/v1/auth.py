"""认证路由：POST /api/login。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.response import ok
from app.db.session import get_db
from app.schemas import LoginRequest
from app.services import get_auth_service

router = APIRouter(tags=["auth"])


@router.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    data = get_auth_service().login(db, req.code, req.userInfo)
    return ok(data)