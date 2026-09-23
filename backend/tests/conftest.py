"""pytest 全局配置：用 SQLite 内存库替代 MySQL，隔离环境跑测试。"""
import os

# 必须在导入 app 之前覆盖配置，避免连接真实 MySQL
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["USE_MOCK_LLM"] = "true"
os.environ["REDIS_URL"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
import app.models  # noqa: F401  注册全部 ORM 模型
from app.main import app as fastapi_app


@pytest.fixture()
def engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture()
def db(engine):
    TestingSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(engine):
    TestingSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    fastapi_app.dependency_overrides[get_db] = override_get_db
    with TestClient(fastapi_app) as client:
        yield client
    fastapi_app.dependency_overrides.clear()


@pytest.fixture()
def auth_token(client) -> str:
    """登录并返回已认证的 token。"""
    resp = client.post("/api/login", json={"code": "test-code-001"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    return data["token"]


@pytest.fixture()
def auth_headers(auth_token) -> dict:
    return {"Authorization": f"Bearer {auth_token}"}