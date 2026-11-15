"""座席管理 API（FastAPI）

ローカル：cd backend → uvicorn app.main:app --reload → http://localhost:8000（画面も一緒に出る）
本番：gunicorn -w 2 -k uvicorn.workers.UvicornWorker app.main:app（infra/main.bicep が設定する）。画面は別の Web App（frontend/）
"""
import base64
import binascii
import logging
import os
import re
from urllib.parse import quote
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from . import knowwho as kw
from . import services as svc
from .auth import current_user, require_admin
from .config import settings
from .db import get_db, lock_row
from .models import (Attachment, Consultation, ConsultationMemo, ConsultationRequest, Expertise, Floor, OpenQuestion, Person, Preference, Presence, Reservation,
                     Room, RoomBooking, Seat)
from .seed_data import STATUSES, TOPICS, match_topics
from .seeding import init_db, reset_demo
from .timeutil import hhmm_now, today

logging.basicConfig(level=logging.INFO)
if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor()
    except Exception as e:
        logging.warning("Application Insights not configured: %s", e)


@asynccontextmanager
async def lifespan(_app):
    init_db(seed=settings.seed_on_start)
    yield


app = FastAPI(title="会いに行ける Know-Who API", version="1.0.0", lifespan=lifespan,
              docs_url="/api/docs" if settings.is_demo else None, redoc_url=None,
              openapi_url="/api/openapi.json" if settings.is_demo else None)
if settings.allowed_origins:
    app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins, allow_methods=["GET", "POST", "PUT", "DELETE"],
                       allow_headers=["Content-Type", "Authorization", "X-Demo-User"])


@app.middleware("http")
async def security_headers(request, call_next):
    res = await call_next(request)
    res.headers.setdefault("X-Content-Type-Options", "nosniff")
    res.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    res.headers.setdefault("X-Frame-Options", "DENY")
    res.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    res.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
    )
    if request.url.path.startswith("/api/"):
        res.headers.setdefault("Cache-Control", "no-store")
    return res


HHMM = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class TimeRange(BaseModel):
    all_day: bool = True
    start: str = "09:00"
    end: str = "18:00"

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not HHMM.match(v):
            raise ValueError("時刻は HH:MM で指定してください")
        return v


class Purpose(BaseModel):
    purpose: str | None = Field(default=None, max_length=100)  # 今日やること
    near_person_id: str | None = None                          # 近くに座りたい人（詳しい人）


class ReservationIn(TimeRange, Purpose):
    seat_id: str
    day: str


class RoomBookingIn(BaseModel):
    day: str
    start: str
    end: str
    title: str = Field(min_length=1, max_length=100)

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not HHMM.match(v):
            raise ValueError("時刻は HH:MM で指定してください")
        return v


class CheckinIn(Purpose):
    seat_id: str


class ConsultIn(BaseModel):
    expert_id: str
    topic: str = Field(min_length=1, max_length=60)
    resolved: bool = True
    source: Literal["seat", "profile"] = "profile"


class StatusIn(BaseModel):
    status: Literal["remote", "trip", "out", "leave", "none"]


def _filled(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("空欄のままでは送れません")
    return v


class SlotIn(BaseModel):
    day: str
    start: str

    @field_validator("start")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not HHMM.match(v):
            raise ValueError("時刻は HH:MM で指定してください")
        return v


class RequestIn(BaseModel):
    expert_id: str
    topic: str = Field(min_length=1, max_length=60)
    purpose: str = Field(min_length=1, max_length=300)    # 知りたいこと
    viewpoint: str = Field(min_length=1, max_length=200)  # 観点
    message: str = Field(min_length=1, max_length=2000)   # 依頼文（テンプレートから作って本人が直せる）
    slots: list[SlotIn] = Field(min_length=1, max_length=3)
    duration: Literal[5, 15, 30] = 30
    _strip = field_validator("topic", "purpose", "viewpoint", "message")(_filled)


class QuickIn(BaseModel):
    """5分相談：相手とテーマだけで依頼する（日時の候補と依頼文はこちらで作る）。"""
    expert_id: str
    topic: str = Field(min_length=1, max_length=60)
    q: str | None = Field(default=None, max_length=300)  # 探したときの言葉
    slots: list[SlotIn] | None = Field(default=None, min_length=1, max_length=3)  # 選んだ日時（なければ候補を自動で作る）


# 資料：1つの依頼に5つまで、1ファイル10MBまで。開いて危なくない形式だけ受け付ける
ATTACH_MAX, ATTACH_COUNT = 10 * 1024 * 1024, 5
ATTACH_TYPES = {
    "pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp",
    "txt": "text/plain", "csv": "text/csv",
    "doc": "application/msword", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint", "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


class AttachmentIn(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    data: str = Field(min_length=1)  # base64（画面からはファイルを読み込んで送る）
    _strip = field_validator("filename")(_filled)


class QuestionIn(BaseModel):
    q: str = Field(min_length=1, max_length=300)
    _strip = field_validator("q")(_filled)


class AcceptIn(BaseModel):
    slot: int = Field(ge=0, le=2)
    mode: Literal["office", "online"] = "office"


class DeclineIn(BaseModel):
    reason: str | None = None


class SeatIn(BaseModel):
    seat_id: str


class StartIn(BaseModel):
    online: bool = False


class FinishIn(BaseModel):
    helpful: bool | None = None
    note: str | None = Field(default=None, max_length=200)  # 「何がわかったか」の一言


class MemoIn(BaseModel):
    heard: str = Field(min_length=1, max_length=2000)    # 聞いたこと
    learned: str = Field(min_length=1, max_length=2000)  # 分かったこと
    reuse: str = Field(default="", max_length=2000)      # 次に使える形（一言メモのときは空でよい）
    helpful: bool | None = None
    _strip = field_validator("heard", "learned")(_filled)


class PrefIn(BaseModel):
    opt_in: bool
    scope: Literal["all", "dept", "private"]
    notify: bool = True


# ---------- 参照 ----------
@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"ok": True}


@app.get("/api/me")
def me(user: Person = Depends(current_user), db: Session = Depends(get_db)):
    ctx = svc.Ctx(db)
    return {"user": ctx.person(user.id), "mode": "demo" if settings.is_demo else "sso", "tenant": settings.tenant_name,
            "today": today().isoformat(), "now": hhmm_now(), "timezone": settings.timezone, "statuses": STATUSES,
            "hours": [settings.open_hour, settings.close_hour], "book_ahead_days": settings.book_ahead_days, "topics": list(TOPICS),
            "prefs": ctx.pref(user.id), "decline_reasons": kw.DECLINE_REASONS}


@app.get("/api/users")
def users(db: Session = Depends(get_db)):
    if not settings.is_demo:
        raise HTTPException(404)
    ctx = svc.Ctx(db)
    return {"users": [ctx.person(pid) for pid in sorted(ctx.people)]}


@app.get("/api/floors")
def floors(_: Person = Depends(current_user), db: Session = Depends(get_db)):
    ctx = svc.Ctx(db)
    out = []
    for f in sorted(ctx.floors.values(), key=lambda f: f.sort):
        out.append({"id": f.id, "name": f.name, "layout": f.layout,
                    "seats": [ctx.seat(s.id) for s in ctx.seats.values() if s.floor_id == f.id and s.active],
                    "rooms": [{"id": r.id, "name": r.name, "capacity": r.capacity, "x": r.x, "y": r.y, "w": r.w, "h": r.h}
                              for r in db.scalars(select(Room).where(Room.floor_id == f.id))]})
    return {"floors": out, "departments": [{"id": k, "name": v} for k, v in ctx.depts.items()]}


@app.get("/api/state")
def state(floor: str, day: str | None = None, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    ctx = svc.Ctx(db, user)
    return svc.floor_state(db, ctx, floor, svc.parse_day(day), user)


@app.get("/api/seats/{sid}")
def seat(sid: str, day: str | None = None, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    return svc.seat_detail(db, svc.Ctx(db, user), sid, svc.parse_day(day))


@app.get("/api/search")
def search(q: str = "", user: Person = Depends(current_user), db: Session = Depends(get_db)):
    q = q.strip()[:50]
    if not q:
        return {"people": [], "seats": [], "rooms": []}
    ctx = svc.Ctx(db, user)
    t = today()
    pres, rsv = svc.presences(db, t), svc.reservations_on(db, t)
    key = q.replace(" ", "").replace("　", "").lower()
    topics = set(match_topics(q))
    people = [pid for pid, p in ctx.people.items()
              if key in p.name.replace(" ", "").lower() or key in p.email.lower() or key in ctx.depts.get(p.dept_id, "").lower() or key in p.title.lower()
              or topics & set(ctx.skills.get(pid, {}))]
    seats = [s.id for s in ctx.seats.values() if key in s.name.lower().replace(" ", "") or key in s.area.lower().replace(" ", "")]
    rooms = list(db.scalars(select(Room)))
    return {"topics": sorted(topics), "people": [{**ctx.person(pid), "location": svc.location(ctx, pid, pres, rsv, t)} for pid in sorted(people)[:30]],
            "seats": [ctx.seat(sid) for sid in sorted(seats)[:10]],
            "rooms": [{"id": r.id, "name": r.name, "floor": r.floor_id, "capacity": r.capacity} for r in rooms if key in r.name.lower()][:10]}


@app.get("/api/people")
def people(dept: str | None = None, day: str | None = None, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    ctx = svc.Ctx(db, user)
    d = svc.parse_day(day)
    pres = svc.presences(db, d) if d == today() else {}
    rsv = svc.reservations_on(db, d)
    ids = sorted(pid for pid, p in ctx.people.items() if not dept or p.dept_id == dept)
    return {"day": d.isoformat(), "people": [{**ctx.person(pid), "location": svc.location(ctx, pid, pres, rsv, d)} for pid in ids]}


@app.get("/api/recommend")
def recommend(q: str = "", day: str | None = None, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    return svc.recommend(db, svc.Ctx(db, user), q.strip()[:100], svc.parse_day(day), user)


def _near(db: Session, user: Person, body: Purpose) -> tuple[str | None, str | None]:
    near = body.near_person_id
    if near and (near == user.id or not db.get(Person, near)):
        raise HTTPException(422, "近くに座りたい人の指定が正しくありません")
    purpose = (body.purpose or "").strip() or None
    return purpose, near


# ---------- 在席 ----------
def _my_presence(db: Session, user: Person) -> Presence | None:
    return db.scalar(select(Presence).where(Presence.person_id == user.id, Presence.day == today()))


@app.post("/api/checkin")
def checkin(body: CheckinIn, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    seat_row = db.get(Seat, body.seat_id)
    if not seat_row or not seat_row.active:
        raise HTTPException(404, "座席が見つかりません")
    t, now = today(), hhmm_now()
    lock_row(db, select(Seat.id).where(Seat.id == seat_row.id))  # 同じ席に同時にチェックインしても二重にならない
    taken = db.scalar(select(Presence).where(Presence.day == t, Presence.seat_id == seat_row.id, Presence.status == "office",
                                             Presence.checked_out_at.is_(None), Presence.person_id != user.id))
    if taken:
        raise HTTPException(409, "この座席はほかの人が使っています")
    others = [r for r in db.scalars(select(Reservation).where(Reservation.day == t, Reservation.seat_id == seat_row.id, Reservation.person_id != user.id))
              if r.all_day or r.end > now]
    if any(r.all_day or r.start <= now < r.end for r in others):
        raise HTTPException(409, "この座席は今の時間、ほかの人が予約しています")
    purpose, near = _near(db, user, body)
    p = _my_presence(db, user) or Presence(person_id=user.id, day=t, status="office")
    p.status, p.seat_id, p.checked_in_at, p.checked_out_at = "office", seat_row.id, datetime.now(timezone.utc), None
    mine = next((r for r in db.scalars(select(Reservation).where(Reservation.day == t, Reservation.person_id == user.id))), None)
    p.purpose = purpose or (mine.purpose if mine and mine.seat_id == seat_row.id else None)
    p.near_person_id = near or (mine.near_person_id if mine and mine.seat_id == seat_row.id else None)
    db.add(p)
    svc.audit(db, user, "presence.checkin", f"seat:{seat_row.id}")
    db.commit()
    soon = next((r for r in others if r.start > now), None)
    return {"ok": True, "warning": f"{soon.start}から予約が入っています" if soon else None}


@app.post("/api/checkout")
def checkout(user: Person = Depends(current_user), db: Session = Depends(get_db)):
    p = _my_presence(db, user)
    if not p or p.status != "office" or p.checked_out_at:
        raise HTTPException(409, "チェックインしていません")
    p.checked_out_at = datetime.now(timezone.utc)
    svc.audit(db, user, "presence.checkout", f"seat:{p.seat_id}")
    db.commit()
    return {"ok": True}


@app.post("/api/status")
def set_status(body: StatusIn, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    p = _my_presence(db, user)
    if body.status == "none":
        if p:
            db.delete(p)
    else:
        p = p or Presence(person_id=user.id, day=today(), status=body.status)
        p.status, p.seat_id, p.checked_in_at, p.checked_out_at = body.status, None, None, None
        db.add(p)
    svc.audit(db, user, "presence.status", f"person:{user.id}", status=body.status)
    db.commit()
    return {"ok": True}


# ---------- 予約 ----------
@app.get("/api/reservations/mine")
def my_reservations(user: Person = Depends(current_user), db: Session = Depends(get_db)):
    ctx = svc.Ctx(db, user)
    rs = db.scalars(select(Reservation).where(Reservation.person_id == user.id, Reservation.day >= today()).order_by(Reservation.day, Reservation.start))
    return {"reservations": [svc.reservation_view(ctx, r) for r in rs]}


def _save_reservation(db: Session, user: Person, body: ReservationIn, existing: Reservation | None) -> Reservation:
    seat_row = db.get(Seat, body.seat_id)
    if not seat_row or not seat_row.active:
        raise HTTPException(404, "座席が見つかりません")
    d = svc.parse_day(body.day)
    start, end = svc.check_booking_window(d, body.start, body.end, body.all_day)
    lock_row(db, select(Seat.id).where(Seat.id == seat_row.id))
    excl = existing.id if existing else -1
    same_seat = [r for r in db.scalars(select(Reservation).where(Reservation.day == d, Reservation.seat_id == seat_row.id, Reservation.id != excl))]
    if any(r.all_day or body.all_day or svc.overlaps(start, end, r.start, r.end) for r in same_seat):
        raise HTTPException(409, "その時間はすでに予約されています")
    mine = db.scalar(select(Reservation.id).where(Reservation.day == d, Reservation.person_id == user.id, Reservation.id != excl))
    if mine:
        raise HTTPException(409, "同じ日にほかの座席を予約しています。予約一覧から変更してください")
    if d == today():
        taken = db.scalar(select(Presence.id).where(Presence.day == d, Presence.seat_id == seat_row.id, Presence.status == "office",
                                                    Presence.checked_out_at.is_(None), Presence.person_id != user.id))
        if taken:
            raise HTTPException(409, "この座席はいまほかの人が使っています")
    purpose, near = _near(db, user, body)
    r = existing or Reservation(person_id=user.id)
    r.seat_id, r.day, r.start, r.end, r.all_day = seat_row.id, d, start, end, body.all_day
    if purpose or near or not existing:
        r.purpose, r.near_person_id = purpose, near
    db.add(r)
    db.flush()
    svc.audit(db, user, "reservation.update" if existing else "reservation.create", f"reservation:{r.id}", seat=seat_row.id, day=d.isoformat())
    db.commit()
    return r


@app.post("/api/reservations", status_code=201)
def create_reservation(body: ReservationIn, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    r = _save_reservation(db, user, body, None)
    return svc.reservation_view(svc.Ctx(db, user), r)


@app.put("/api/reservations/{rid}")
def update_reservation(rid: int, body: ReservationIn, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    r = db.get(Reservation, rid)
    if not r or r.person_id != user.id:
        raise HTTPException(404, "予約が見つかりません")
    r = _save_reservation(db, user, body, r)
    return svc.reservation_view(svc.Ctx(db, user), r)


@app.delete("/api/reservations/{rid}")
def delete_reservation(rid: int, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    r = db.get(Reservation, rid)
    if not r or (r.person_id != user.id and not user.is_admin):
        raise HTTPException(404, "予約が見つかりません")
    if r.day < today():
        raise HTTPException(409, "過去の予約は取り消せません")
    svc.audit(db, user, "reservation.delete", f"reservation:{r.id}", seat=r.seat_id, day=r.day.isoformat())
    db.delete(r)
    db.commit()
    return {"ok": True}


# ---------- 相談の記録（Know-Who を自然に貯める） ----------
@app.post("/api/consultations", status_code=201)
def consult(body: ConsultIn, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    if body.expert_id == user.id:
        raise HTTPException(422, "自分への相談は記録できません")
    if not db.get(Person, body.expert_id):
        raise HTTPException(404, "相手が見つかりません")
    topic, t = body.topic.strip(), today()
    c = db.scalar(select(Consultation).where(Consultation.asker_id == user.id, Consultation.expert_id == body.expert_id,
                                             Consultation.day == t, Consultation.topic == topic))
    if not c:
        c = Consultation(asker_id=user.id, expert_id=body.expert_id, topic=topic, day=t)
        db.add(c)
    c.resolved, c.source = body.resolved, body.source
    learned = False
    if body.resolved and not db.scalar(select(Expertise.id).where(Expertise.person_id == body.expert_id, Expertise.topic == topic)):
        db.add(Expertise(person_id=body.expert_id, topic=topic, learned=True))  # 相談で解決した分野は、その人の「聞けること」に自動で加わる
        learned = True
    db.flush()
    svc.audit(db, user, "consultation.record", f"person:{body.expert_id}", topic=topic, resolved=body.resolved, source=body.source)
    db.commit()
    helped = db.scalar(select(func.count(Consultation.id)).where(Consultation.expert_id == body.expert_id, Consultation.topic == topic,
                                                                 Consultation.resolved.is_(True)))
    return {"ok": True, "helped": helped, "learned": learned}


# ---------- 会いに行ける Know-Who：見つける ----------
@app.get("/api/knowwho")
def knowwho_search(q: str = "", user: Person = Depends(current_user), db: Session = Depends(get_db)):
    q = q.strip()[:300]
    out = kw.search(db, svc.Ctx(db, user), q, user)
    if out["topics"]:  # 推薦後の相談依頼率（H-1）の分母
        svc.audit(db, user, "knowwho.search", f"topics:{','.join(out['topics'])}", candidates=len(out["candidates"]))
    if q and not out["candidates"]:  # 知識の空白として経営画面に回す
        svc.audit(db, user, "knowwho.gap", "q", q=q[:100], topics=out["topics"])
    db.commit()
    return out


# ---------- 詳しい人がいないときに置いておく質問 ----------
@app.post("/api/questions", status_code=201)
def post_question(body: QuestionIn, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    ctx = svc.Ctx(db, user)
    oq = db.scalar(select(OpenQuestion).where(OpenQuestion.asker_id == user.id, OpenQuestion.q == body.q, OpenQuestion.status != "closed"))
    if not oq:
        oq = OpenQuestion(asker_id=user.id, q=body.q)
        db.add(oq)
        db.flush()
        svc.audit(db, user, "question.post", f"question:{oq.id}")
    db.commit()
    return kw.question_view(ctx, oq)


@app.get("/api/questions")
def my_questions(user: Person = Depends(current_user), db: Session = Depends(get_db)):
    ctx = svc.Ctx(db, user)
    kw.check_questions(db, ctx, user)
    rows = db.scalars(select(OpenQuestion).where(OpenQuestion.asker_id == user.id, OpenQuestion.status != "closed").order_by(OpenQuestion.created_at.desc()))
    return {"questions": [kw.question_view(ctx, q) for q in rows]}


@app.post("/api/questions/{qid}/close")
def close_question(qid: int, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    oq = db.get(OpenQuestion, qid)
    if not oq or oq.asker_id != user.id:
        raise HTTPException(404, "質問が見つかりません")
    oq.status = "closed"
    db.commit()
    return {"ok": True}


@app.get("/api/people/{pid}/profile")
def person_profile(pid: str, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    if not db.get(Person, pid):
        raise HTTPException(404, "相手が見つかりません")
    return kw.profile(db, svc.Ctx(db, user), pid, user)


@app.post("/api/knowledge/{mid}/view")
def view_knowledge(mid: int, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    m = db.get(ConsultationMemo, mid)
    if not m or m.status != "approved":
        raise HTTPException(404, "見つかりません")
    r = db.get(ConsultationRequest, m.request_id)
    if user.id not in (r.asker_id, r.expert_id):  # 次の相談者に参照された回数（H-5）
        m.views += 1
        db.commit()
    return {"views": m.views}


# ---------- 設定（FR-12） ----------
@app.get("/api/prefs")
def get_prefs(user: Person = Depends(current_user), db: Session = Depends(get_db)):
    return svc.Ctx(db).pref(user.id)


@app.put("/api/prefs")
def put_prefs(body: PrefIn, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    p = db.get(Preference, user.id) or Preference(person_id=user.id)
    p.opt_in, p.scope, p.notify, p.confirmed, p.updated_at = body.opt_in, body.scope, body.notify, True, datetime.now(timezone.utc)
    db.add(p)
    svc.audit(db, user, "prefs.update", f"person:{user.id}", opt_in=body.opt_in, scope=body.scope)
    db.commit()
    return svc.Ctx(db).pref(user.id)


# ---------- 話す：相談の依頼（FR-05〜11） ----------
def _request(db: Session, rid: int, user: Person) -> ConsultationRequest:
    r = db.get(ConsultationRequest, rid)
    if not r or user.id not in (r.asker_id, r.expert_id):
        raise HTTPException(404, "相談が見つかりません")
    return r


def _view(db: Session, r: ConsultationRequest, user: Person) -> dict:
    return kw.request_view(db, svc.Ctx(db, user), r, user)


@app.get("/api/requests")
def list_requests(user: Person = Depends(current_user), db: Session = Depends(get_db)):
    ctx = svc.Ctx(db, user)
    rows = db.scalars(select(ConsultationRequest).where((ConsultationRequest.asker_id == user.id) | (ConsultationRequest.expert_id == user.id))
                      .order_by(ConsultationRequest.created_at.desc()))
    views = [kw.request_view(db, ctx, r, user, full=False) for r in rows]
    return {"received": [v for v in views if v["role"] == "expert"], "sent": [v for v in views if v["role"] == "asker"], "inbox": kw.inbox(db, ctx, user)}


@app.get("/api/inbox")
def get_inbox(user: Person = Depends(current_user), db: Session = Depends(get_db)):
    return kw.inbox(db, svc.Ctx(db, user), user)


@app.get("/api/requests/{rid}")
def get_request(rid: int, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    return _view(db, _request(db, rid, user), user)


@app.post("/api/requests", status_code=201)
def create_request(body: RequestIn, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    return _create_request(db, user, body)


@app.post("/api/requests/quick", status_code=201)
def quick_request(body: QuickIn, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    """5分相談をお願いする：日時の候補（相手の出社日を優先）と依頼文を作って、そのまま送る。"""
    expert = db.get(Person, body.expert_id)
    if not expert:
        raise HTTPException(404, "相手が見つかりません")
    slots = [s.model_dump() for s in body.slots] if body.slots else kw.quick_slots(db, svc.Ctx(db, user), expert.id, 5)
    purpose = (body.q or "").strip() or f"{body.topic}について聞きたい"
    msg = (f"{expert.name}さん\n\n{body.topic}について、5分だけ教えていただけないでしょうか。\n知りたいこと：{purpose}\n"
           "候補の日時から選んでいただければ、近くの席を取って伺います。")
    return _create_request(db, user, RequestIn(expert_id=expert.id, topic=body.topic, purpose=purpose[:300], viewpoint="まずは5分だけ",
                                               message=msg, slots=[SlotIn(**s) for s in slots], duration=5))


@app.get("/api/requests/quick/slots")
def quick_slot_suggestions(expert_id: str, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    """5分相談の日時の候補（相手の出社日を優先）。画面で選び直してから送れる。"""
    if not db.get(Person, expert_id):
        raise HTTPException(404, "相手が見つかりません")
    return {"slots": kw.quick_slots(db, svc.Ctx(db, user), expert_id, 5)}


def _create_request(db: Session, user: Person, body: RequestIn) -> dict:
    if body.expert_id == user.id:
        raise HTTPException(422, "自分には依頼できません")
    if not db.get(Person, body.expert_id):
        raise HTTPException(404, "相手が見つかりません")
    lo, hi = settings.open_hour * 60, settings.close_hour * 60
    slots = []
    for s in body.slots:
        d = svc.parse_day(s.day)
        if d < today() or d > today() + timedelta(days=settings.book_ahead_days):
            raise HTTPException(422, f"希望日は今日から{settings.book_ahead_days}日先までで選んでください")
        h, m = map(int, s.start.split(":"))
        if not (lo <= h * 60 + m and h * 60 + m + body.duration <= hi):
            raise HTTPException(422, f"希望の時間は{settings.open_hour}時〜{settings.close_hour}時の間で選んでください")
        if {"day": d.isoformat(), "start": s.start} not in slots:
            slots.append({"day": d.isoformat(), "start": s.start})
    r = ConsultationRequest(asker_id=user.id, expert_id=body.expert_id, topic=body.topic, purpose=body.purpose, viewpoint=body.viewpoint,
                            message=body.message, slots=slots, duration=body.duration)
    db.add(r)
    db.flush()
    svc.audit(db, user, "request.create", f"request:{r.id}", expert=body.expert_id, topic=body.topic)
    db.commit()
    return _view(db, r, user)


@app.post("/api/requests/{rid}/accept")
def accept_request(rid: int, body: AcceptIn, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    r = _request(db, rid, user)
    if r.expert_id != user.id:
        raise HTTPException(403, "依頼を受けた人だけが返事できます")
    if r.status != "pending":
        raise HTTPException(409, "この依頼にはもう返事しています")
    if body.slot >= len(r.slots):
        raise HTTPException(422, "日時の候補を選んでください")
    s = r.slots[body.slot]
    if date.fromisoformat(s["day"]) < today():
        raise HTTPException(422, "過ぎた日は選べません。依頼した人に別の日を出してもらってください")
    r.status, r.mode, r.day, r.start, r.responded_at = "accepted", body.mode, date.fromisoformat(s["day"]), s["start"], datetime.now(timezone.utc)
    svc.audit(db, user, "request.accept", f"request:{r.id}", day=s["day"], start=s["start"], mode=body.mode)
    db.commit()
    if r.mode == "office":
        _auto_seat(db, r)
    return _view(db, r, user)


def _auto_seat(db: Session, r: ConsultationRequest) -> None:
    """承諾と同時に、依頼した人の席を相手の近くに確保する（相談の時間を含む30分以上）。取れなければ、あとで本人が選ぶ。"""
    asker = db.get(Person, r.asker_id)
    if db.scalar(select(Reservation.id).where(Reservation.person_id == asker.id, Reservation.day == r.day)):
        return  # その日はもう席を取っている
    ctx = svc.Ctx(db, asker)
    ex = kw.find_seat_of(db, ctx, r.expert_id, r.day)
    if not ex:
        return
    for seat in kw.nearby_seats(db, ctx, asker, ex, r.day):
        h, m = map(int, r.start.split(":"))
        t0 = h * 60 + m - m % 30
        t1 = min(settings.close_hour * 60, t0 + max(30, ((r.duration + m % 30 + 29) // 30) * 30))
        body = ReservationIn(seat_id=seat["id"], day=r.day.isoformat(), all_day=False, start=f"{t0 // 60:02d}:{t0 % 60:02d}", end=f"{t1 // 60:02d}:{t1 % 60:02d}",
                             purpose=f"{r.topic}の相談"[:100], near_person_id=r.expert_id)
        try:
            res = _save_reservation(db, asker, body, None)
        except HTTPException:
            db.rollback()
            continue
        r = db.get(ConsultationRequest, r.id)
        r.reservation_id = res.id
        svc.audit(db, asker, "request.autoseat", f"request:{r.id}", seat=seat["id"])
        db.commit()
        return


@app.post("/api/requests/{rid}/decline")
def decline_request(rid: int, body: DeclineIn, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    r = _request(db, rid, user)
    if r.expert_id != user.id:
        raise HTTPException(403, "依頼を受けた人だけが返事できます")
    if r.status != "pending":
        raise HTTPException(409, "この依頼にはもう返事しています")
    if body.reason and body.reason not in kw.DECLINE_REASONS:
        raise HTTPException(422, "辞退の理由を選び直してください")
    r.status, r.decline_reason, r.responded_at = "declined", body.reason, datetime.now(timezone.utc)
    svc.audit(db, user, "request.decline", f"request:{r.id}", reason=body.reason)
    db.commit()
    return _view(db, r, user)


@app.post("/api/requests/{rid}/cancel")
def cancel_request(rid: int, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    r = _request(db, rid, user)
    if r.status not in ("pending", "accepted") or r.started_at:
        raise HTTPException(409, "この相談は取り消せません")
    r.status = "cancelled"
    if r.reservation_id and (res := db.get(Reservation, r.reservation_id)) and res.day >= today():
        db.delete(res)  # 相談のために取った席は空ける
    r.reservation_id = None
    svc.audit(db, user, "request.cancel", f"request:{r.id}")
    db.commit()
    return _view(db, r, user)


@app.post("/api/requests/{rid}/seat")
def request_seat(rid: int, body: SeatIn, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    r = _request(db, rid, user)
    if r.asker_id != user.id:
        raise HTTPException(403, "席は依頼した人が取ります")
    if r.status != "accepted" or r.mode != "office" or not r.day or r.day < today():
        raise HTTPException(409, "この相談では席を取れません")
    mine = db.scalar(select(Reservation).where(Reservation.person_id == user.id, Reservation.day == r.day))
    body_in = ReservationIn(seat_id=body.seat_id, day=r.day.isoformat(), all_day=True, purpose=f"{r.topic}の相談"[:100], near_person_id=r.expert_id)
    res = _save_reservation(db, user, body_in, mine)  # 同じ日にほかの席を取っていれば、こちらへ移す
    r.reservation_id = res.id
    svc.audit(db, user, "request.seat", f"request:{r.id}", seat=body.seat_id)
    db.commit()
    return _view(db, r, user)


@app.post("/api/requests/{rid}/online")
def request_online(rid: int, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    """出社日が合わないときは、オンライン相談に切り替える（座席は取らない）。"""
    r = _request(db, rid, user)
    if r.status != "accepted" or r.started_at:
        raise HTTPException(409, "この相談は切り替えられません")
    r.mode = "online"
    if r.reservation_id and (res := db.get(Reservation, r.reservation_id)) and res.day >= today():
        db.delete(res)
    r.reservation_id = None
    svc.audit(db, user, "request.online", f"request:{r.id}")
    db.commit()
    return _view(db, r, user)


@app.post("/api/requests/{rid}/start")
def start_request(rid: int, body: StartIn, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    """当日のチェックイン。相談の「開始」として扱い、双方に残り時間を出す（FR-09）。"""
    r = _request(db, rid, user)
    if r.status != "accepted":
        raise HTTPException(409, "受けてもらった相談だけ始められます")
    if r.day != today():
        raise HTTPException(409, "相談の日になったら始められます")
    if body.online:
        r.mode = "online"
    warning = None
    if r.mode == "office" and user.id == r.asker_id and r.reservation_id:
        res = db.get(Reservation, r.reservation_id)
        p = _my_presence(db, user)
        if res and res.day == today() and not (p and p.status == "office" and p.seat_id == res.seat_id and not p.checked_out_at):
            warning = checkin(CheckinIn(seat_id=res.seat_id, purpose=f"{r.topic}の相談"[:100], near_person_id=r.expert_id), user, db)["warning"]
    if not r.started_at:
        r.started_at = datetime.now(timezone.utc)
    svc.audit(db, user, "request.start", f"request:{r.id}", mode=r.mode)
    db.commit()
    return {**_view(db, r, user), "warning": warning}


@app.post("/api/requests/{rid}/finish")
def finish_request(rid: int, body: FinishIn, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    r = _request(db, rid, user)
    if r.status != "accepted" or not r.started_at:
        raise HTTPException(409, "始めた相談だけ終えられます")
    r.status, r.ended_at = "done", datetime.now(timezone.utc)
    if user.id == r.asker_id and body.helpful is not None:
        r.helpful = body.helpful
    note = (body.note or "").strip()
    if note and not db.scalar(select(ConsultationMemo.id).where(ConsultationMemo.request_id == r.id)):
        # 一言メモ：受けた人が承認するまで公開しない（NF-11）
        db.add(ConsultationMemo(request_id=r.id, author_id=user.id, heard=r.purpose, learned=note, reuse=""))
    kw.record_consultation(db, r)
    svc.audit(db, user, "request.finish", f"request:{r.id}", note=bool(note))
    db.commit()
    return _view(db, r, user)


@app.put("/api/requests/{rid}/memo")
def save_memo(rid: int, body: MemoIn, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    """相談メモ。依頼者と受諾者の双方が確認・修正できる。承認されるまで公開しない（FR-10・11）。"""
    r = _request(db, rid, user)
    if not (r.status == "done" or (r.status == "accepted" and r.started_at)):
        raise HTTPException(409, "相談を始めてから書けます")
    m = db.scalar(select(ConsultationMemo).where(ConsultationMemo.request_id == r.id))
    if m and m.status == "approved":
        raise HTTPException(409, "承認済みのメモは直せません")
    m = m or ConsultationMemo(request_id=r.id, author_id=user.id)
    m.heard, m.learned, m.reuse, m.updated_at = body.heard, body.learned, body.reuse, datetime.now(timezone.utc)
    db.add(m)
    if user.id == r.asker_id and body.helpful is not None:
        r.helpful = body.helpful
    if r.status == "accepted":  # メモを書いた＝相談をした
        r.status, r.ended_at = "done", datetime.now(timezone.utc)
    kw.record_consultation(db, r)
    svc.audit(db, user, "memo.save", f"request:{r.id}")
    db.commit()
    return _view(db, r, user)


# ---------- 相談につける資料 ----------
@app.post("/api/requests/{rid}/attachments", status_code=201)
def add_attachment(rid: int, body: AttachmentIn, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    r = _request(db, rid, user)
    if r.status in ("declined", "cancelled"):
        raise HTTPException(409, "この相談には資料を追加できません")
    name = os.path.basename(body.filename.replace("\\", "/")).strip()[:200] or "資料"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in ATTACH_TYPES:
        raise HTTPException(422, "PDF・Word・Excel・PowerPoint・画像・テキスト・CSV のファイルを選んでください")
    try:
        data = base64.b64decode(body.data, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "ファイルを読み込めませんでした")
    if not data:
        raise HTTPException(422, "空のファイルは添付できません")
    if len(data) > ATTACH_MAX:
        raise HTTPException(413, "1つのファイルは10MBまでです")
    if db.scalar(select(func.count(Attachment.id)).where(Attachment.request_id == r.id)) >= ATTACH_COUNT:
        raise HTTPException(409, f"資料は1つの相談に{ATTACH_COUNT}つまでです")
    a = Attachment(request_id=r.id, uploader_id=user.id, filename=name, content_type=ATTACH_TYPES[ext], size=len(data), data=data)
    db.add(a)
    db.flush()
    svc.audit(db, user, "attachment.add", f"request:{r.id}", attachment=a.id, size=len(data))
    db.commit()
    return kw.attachment_view(svc.Ctx(db, user), a)


def _attachment(db: Session, aid: int, user: Person) -> Attachment:
    a = db.get(Attachment, aid)
    if not a:
        raise HTTPException(404, "資料が見つかりません")
    _request(db, a.request_id, user)  # 当事者でなければ 404
    return a


@app.get("/api/attachments/{aid}")
def get_attachment(aid: int, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    a = _attachment(db, aid, user)
    svc.audit(db, user, "attachment.download", f"attachment:{a.id}")
    db.commit()
    return Response(content=a.data, media_type=a.content_type, headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(a.filename)}", "Cache-Control": "no-store"})


@app.delete("/api/attachments/{aid}")
def delete_attachment(aid: int, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    a = _attachment(db, aid, user)
    if a.uploader_id != user.id:
        raise HTTPException(403, "資料を消せるのは、つけた人だけです")
    svc.audit(db, user, "attachment.delete", f"attachment:{a.id}")
    db.delete(a)
    db.commit()
    return {"ok": True}


@app.post("/api/requests/{rid}/memo/approve")
def approve_memo(rid: int, body: MemoIn | None = None, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    r = _request(db, rid, user)
    if r.expert_id != user.id:
        raise HTTPException(403, "相談を受けた人だけが承認できます")
    m = db.scalar(select(ConsultationMemo).where(ConsultationMemo.request_id == r.id))
    if not m or m.status != "pending":
        raise HTTPException(409, "承認待ちのメモがありません")
    if body:
        m.heard, m.learned, m.reuse = body.heard, body.learned, body.reuse
    m.status, m.approved_by, m.approved_at = "approved", user.id, datetime.now(timezone.utc)
    learned = kw.learn_topic(db, r)
    svc.audit(db, user, "memo.approve", f"request:{r.id}", topic=r.topic)
    db.commit()
    return {**_view(db, r, user), "learned": learned}


# ---------- 会議室 ----------
@app.get("/api/rooms/{room_id}")
def room(room_id: str, day: str | None = None, _: Person = Depends(current_user), db: Session = Depends(get_db)):
    r = db.get(Room, room_id)
    if not r:
        raise HTTPException(404, "会議室が見つかりません")
    ctx = svc.Ctx(db)
    d = svc.parse_day(day)
    bks = sorted(db.scalars(select(RoomBooking).where(RoomBooking.room_id == r.id, RoomBooking.day == d)), key=lambda b: b.start)
    return {"room": {"id": r.id, "name": r.name, "capacity": r.capacity, "floor": r.floor_id, "floor_name": ctx.floors[r.floor_id].name},
            "day": d.isoformat(), "bookings": [{"id": b.id, "start": b.start, "end": b.end, "title": b.title, "organizer": ctx.person(b.organizer_id)} for b in bks]}


@app.post("/api/rooms/{room_id}/bookings", status_code=201)
def book_room(room_id: str, body: RoomBookingIn, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    r = db.get(Room, room_id)
    if not r:
        raise HTTPException(404, "会議室が見つかりません")
    d = svc.parse_day(body.day)
    start, end = svc.check_booking_window(d, body.start, body.end, False)
    if d == today() and start < hhmm_now()[:4] + "0":
        raise HTTPException(422, "過ぎた時間は予約できません")
    lock_row(db, select(Room.id).where(Room.id == r.id))
    clash = [b for b in db.scalars(select(RoomBooking).where(RoomBooking.room_id == r.id, RoomBooking.day == d)) if svc.overlaps(start, end, b.start, b.end)]
    if clash:
        raise HTTPException(409, f"{clash[0].start}〜{clash[0].end}に予約が入っています")
    b = RoomBooking(room_id=r.id, organizer_id=user.id, day=d, start=start, end=end, title=body.title.strip())
    db.add(b)
    db.flush()
    svc.audit(db, user, "room.book", f"room:{r.id}", booking=b.id)
    db.commit()
    return {"id": b.id}


# ---------- 分析 ----------
@app.get("/api/analytics")
def analytics(days: int = 30, _: Person = Depends(require_admin), db: Session = Depends(get_db)):
    days = max(7, min(days, 90))
    return {**svc.analytics(db, svc.Ctx(db), days), "cycle": kw.cycle(db, days)}


@app.post("/api/admin/reset")
def admin_reset():
    if not settings.is_demo:
        raise HTTPException(404)
    reset_demo()
    return {"ok": True}


# 本番は画面（frontend/）を別の Web App に置く。手元で1つのコマンドで試せるよう、画面のフォルダがあれば配る
FRONT = Path(__file__).resolve().parents[2] / "frontend" / "public"
if FRONT.exists() and os.getenv("SERVE_FRONTEND", "true").lower() != "false":
    app.mount("/", StaticFiles(directory=FRONT, html=True), name="front")
else:
    @app.get("/")
    def root():
        return {"service": "会いに行ける Know-Who API", "docs": "/api/docs" if settings.is_demo else None}
