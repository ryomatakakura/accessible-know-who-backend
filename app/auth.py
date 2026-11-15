"""本人の判定。

AUTH_MODE=sso：App Service 認証（Easy Auth・Entra ID）が付ける X-MS-CLIENT-PRINCIPAL-NAME（UPN／メール）で本人を決める。
  App Service は外から送られてきた同名ヘッダーを取り除くので、App Service の認証を「必須」にした上でだけ信頼できる。
AUTH_MODE=demo：画面の切り替えで送る X-Demo-User を使う。本番では絶対に使わない。
"""
import base64
import json

from fastapi import Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import Person


def _principal_name(request: Request) -> str | None:
    name = request.headers.get("x-ms-client-principal-name")
    if name:
        return name
    raw = request.headers.get("x-ms-client-principal")
    if not raw:
        return None
    try:
        data = json.loads(base64.b64decode(raw))
        for c in data.get("claims", []):
            if c.get("typ") in ("preferred_username", "upn", "email",
                                "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn"):
                return c.get("val")
    except Exception:
        return None
    return None


def current_user(request: Request, db: Session = Depends(get_db)) -> Person:
    if settings.auth_mode == "sso":
        name = _principal_name(request)
        if not name:
            raise HTTPException(401, "ログインが必要です")
        person = db.scalar(select(Person).where(func.lower(Person.email) == name.lower()))
        if not person:
            raise HTTPException(403, "このアカウントは、この組織の利用者として登録されていません。管理者に連絡してください。")
        return person
    uid = request.headers.get("x-demo-user") or settings.demo_default_user
    person = db.get(Person, uid)
    if not person:
        raise HTTPException(401, "デモ用の利用者が見つかりません")
    return person


def require_admin(user: Person = Depends(current_user)) -> Person:
    if not user.is_admin:
        raise HTTPException(403, "この画面は管理者だけが見られます")
    return user
