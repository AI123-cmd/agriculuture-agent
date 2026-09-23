"""认证相关模型。"""

from pydantic import BaseModel, Field


class UserInfoIn(BaseModel):
    nickName: str = ""
    avatarUrl: str = ""
    gender: int = 0
    country: str = ""
    province: str = ""
    city: str = ""


class LoginRequest(BaseModel):
    code: str = Field(..., description="wx.login() 返回的临时凭证")
    userInfo: UserInfoIn | None = None


class LoginResponse(BaseModel):
    token: str
    userId: str
    isNewUser: bool
