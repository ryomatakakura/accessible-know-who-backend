"""座席管理 API（FastAPI）

ローカル：cd backend → uvicorn app.main:app --reload → http://localhost:8000（画面も一緒に出る）
本番：gunicorn -w 2 -k uvicorn.workers.UvicornWorker app.main:app（infra/main.bicep が設定する）。画面は別の Web App（frontend/）
"""
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from . import services as svc
from .auth import current_user, require_admin
from .config import settings
from .db import get_db, lock_row
from .models import Consultation, Expertise, Floor, Person, Presence, Reservation, Room, RoomBooking, Seat
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


app = FastAPI(title="座席管理 API", version="1.0.0", lifespan=lifespan,
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
            "hours": [settings.open_hour, settings.close_hour], "book_ahead_days": settings.book_ahead_days, "topics": list(TOPICS)}


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
    ctx = svc.Ctx(db)
    return svc.floor_state(db, ctx, floor, svc.parse_day(day), user)


@app.get("/api/seats/{sid}")
def seat(sid: str, day: str | None = None, _: Person = Depends(current_user), db: Session = Depends(get_db)):
    return svc.seat_detail(db, svc.Ctx(db), sid, svc.parse_day(day))


@app.get("/api/search")
def search(q: str = "", user: Person = Depends(current_user), db: Session = Depends(get_db)):
    q = q.strip()[:50]
    if not q:
        return {"people": [], "seats": [], "rooms": []}
    ctx = svc.Ctx(db)
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
def people(dept: str | None = None, day: str | None = None, _: Person = Depends(current_user), db: Session = Depends(get_db)):
    ctx = svc.Ctx(db)
    d = svc.parse_day(day)
    pres = svc.presences(db, d) if d == today() else {}
    rsv = svc.reservations_on(db, d)
    ids = sorted(pid for pid, p in ctx.people.items() if not dept or p.dept_id == dept)
    return {"day": d.isoformat(), "people": [{**ctx.person(pid), "location": svc.location(ctx, pid, pres, rsv, d)} for pid in ids]}


@app.get("/api/recommend")
def recommend(q: str = "", day: str | None = None, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    return svc.recommend(db, svc.Ctx(db), q.strip()[:100], svc.parse_day(day), user)


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
    ctx = svc.Ctx(db)
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
    return svc.reservation_view(svc.Ctx(db), r)


@app.put("/api/reservations/{rid}")
def update_reservation(rid: int, body: ReservationIn, user: Person = Depends(current_user), db: Session = Depends(get_db)):
    r = db.get(Reservation, rid)
    if not r or r.person_id != user.id:
        raise HTTPException(404, "予約が見つかりません")
    r = _save_reservation(db, user, body, r)
    return svc.reservation_view(svc.Ctx(db), r)


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
    return svc.analytics(db, svc.Ctx(db), max(7, min(days, 90)))


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
        return {"service": "座席ナビ API", "docs": "/api/docs" if settings.is_demo else None}
