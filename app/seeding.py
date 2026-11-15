"""テーブル作成と初期データ投入。複数ワーカーが同時に起動しても二重に入らないようにする。"""
import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

from . import seed_data as S
from .config import settings
from .db import Base, SessionLocal, engine
from .models import (Attachment, AuditLog, Consultation, ConsultationMemo, ConsultationRequest, Department, Expertise, Floor, Person, Preference, Presence,
                     Reservation, Room, RoomBooking, Seat)
from .timeutil import today

log = logging.getLogger(__name__)
LOCK_ID = 424242


def _with_lock(fn):
    """複数のワーカーが同時に起動しても、初期データを二重に入れない。"""
    name = engine.dialect.name
    if name not in ("mysql", "postgresql"):
        return fn()
    get, release = (("SELECT GET_LOCK('seatnavi_seed', 120)", "SELECT RELEASE_LOCK('seatnavi_seed')") if name == "mysql"
                    else (f"SELECT pg_advisory_lock({LOCK_ID})", f"SELECT pg_advisory_unlock({LOCK_ID})"))
    with engine.connect() as conn:
        conn.execute(text(get))
        try:
            return fn()
        finally:
            conn.execute(text(release))
            conn.commit()


def _insert_static(db):
    db.add_all(Department(id=i, name=n) for i, n in S.DEPARTMENTS)
    db.flush()
    db.add_all(Person(id=p["id"], name=p["name"], email=p["email"], dept_id=p["dept"], title=p["title"], color=p["color"], is_admin=p["is_admin"])
               for p in S.people())
    for f in S.FLOORS:
        db.add(Floor(id=f["id"], name=f["name"], sort=f["sort"], layout=f["layout"]))
    db.flush()
    for f in S.FLOORS:
        db.add_all(Seat(floor_id=f["id"], **s) for s in f["seats"])
        db.add_all(Room(floor_id=f["id"], **r) for r in f["rooms"])
    db.flush()
    db.add_all(Expertise(**e) for e in S.expertise())


def _insert_dynamic(db):
    pres, rsv, bks, cons = S.dynamic(today(), ZoneInfo(settings.timezone))
    db.add_all(Presence(**p) for p in pres)
    db.add_all(Reservation(**r) for r in rsv)
    db.add_all(RoomBooking(**b) for b in bks)
    db.add_all(Consultation(**c) for c in cons)
    for r in S.requests(today(), ZoneInfo(settings.timezone)):
        memo = r.pop("memo", None)
        req = ConsultationRequest(**r)
        db.add(req)
        if memo:
            db.flush()
            approved = memo["status"] == "approved"
            db.add(ConsultationMemo(request_id=req.id, **memo, approved_at=r["started_at"] + timedelta(days=1) if approved else None))


# 日付に合わせて作り直すもの（消す順。メモ → 依頼 の順に消す）
DAILY = (Attachment, ConsultationMemo, ConsultationRequest, Presence, Reservation, RoomBooking, Consultation)


def init_db(seed: bool = True) -> None:
    def run():
        try:
            Base.metadata.create_all(engine)
        except (OperationalError, ProgrammingError) as e:
            log.info("create_all skipped: %s", e)
        if not seed:
            return
        with SessionLocal() as db:
            try:
                if not db.scalar(select(Department.id).limit(1)):
                    _insert_static(db)
                    db.flush()
                if not db.scalar(select(Preference.person_id).limit(1)):  # 本人が変えた設定は、日をまたいでも消さない
                    db.add_all(Preference(**p) for p in S.PREFS)
                # デモ：その日のデータがなければ作る（翌日に起動し直しても画面が空にならない）
                if not db.scalar(select(Presence.id).where(Presence.day == today()).limit(1)):
                    for model in DAILY:
                        db.execute(delete(model))
                    db.execute(delete(Expertise).where(Expertise.learned.is_(True)))
                    _insert_dynamic(db)
                db.commit()
            except IntegrityError:
                db.rollback()

    _with_lock(run)


def reset_demo() -> None:
    def run():
        with SessionLocal() as db:
            for model in (*DAILY, AuditLog):
                db.execute(delete(model))
            db.execute(delete(Expertise).where(Expertise.learned.is_(True)))
            _insert_dynamic(db)
            db.commit()

    _with_lock(run)
