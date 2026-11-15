from datetime import date, datetime, timezone

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Department(Base):
    __tablename__ = "departments"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))


class Person(Base):
    __tablename__ = "people"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    dept_id: Mapped[str] = mapped_column(ForeignKey("departments.id"))
    title: Mapped[str] = mapped_column(String(100), default="")
    color: Mapped[str] = mapped_column(String(10), default="#5b6fd6")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)  # 分析画面を見られる


class Floor(Base):
    __tablename__ = "floors"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    sort: Mapped[int] = mapped_column(Integer, default=0)
    layout: Mapped[dict] = mapped_column(JSON, default=dict)  # 壁・エリア・家具など描画だけに使う情報


class Seat(Base):
    __tablename__ = "seats"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    floor_id: Mapped[str] = mapped_column(ForeignKey("floors.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    area: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(30))  # desk / booth / lounge
    features: Mapped[list] = mapped_column(JSON, default=list)  # モニター、窓側 など
    x: Mapped[float] = mapped_column()
    y: Mapped[float] = mapped_column()
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Room(Base):
    __tablename__ = "rooms"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    floor_id: Mapped[str] = mapped_column(ForeignKey("floors.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    capacity: Mapped[int] = mapped_column(Integer)
    x: Mapped[float] = mapped_column()
    y: Mapped[float] = mapped_column()
    w: Mapped[float] = mapped_column()
    h: Mapped[float] = mapped_column()


class Presence(Base):
    """1人1日1行。status：office（seat_idあり）/ remote / trip / out / leave"""
    __tablename__ = "presence"
    __table_args__ = (UniqueConstraint("person_id", "day"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(20))
    seat_id: Mapped[str | None] = mapped_column(ForeignKey("seats.id"), nullable=True, index=True)
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checked_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    purpose: Mapped[str | None] = mapped_column(String(100), nullable=True)  # 今日やること
    near_person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)  # 近くに座りたかった人


class Reservation(Base):
    __tablename__ = "reservations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    seat_id: Mapped[str] = mapped_column(ForeignKey("seats.id"), index=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    start: Mapped[str] = mapped_column(String(5))  # "09:00"
    end: Mapped[str] = mapped_column(String(5))
    all_day: Mapped[bool] = mapped_column(Boolean, default=True)
    purpose: Mapped[str | None] = mapped_column(String(100), nullable=True)
    near_person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Expertise(Base):
    """その人に聞けること。本番は Shpica の知見データ（資料・回答の実績）から同期する想定。"""
    __tablename__ = "expertise"
    __table_args__ = (UniqueConstraint("person_id", "topic"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    topic: Mapped[str] = mapped_column(String(60), index=True)
    docs: Mapped[int] = mapped_column(Integer, default=0)     # 関わった資料の数
    answers: Mapped[int] = mapped_column(Integer, default=0)  # 質問に答えた数
    learned: Mapped[bool] = mapped_column(Boolean, default=False)  # 相談の記録から自動で付いた


class Consultation(Base):
    """だれが、だれに、何を相談し、解決したか。座席から始まったものは source=seat。"""
    __tablename__ = "consultations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asker_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    expert_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    topic: Mapped[str] = mapped_column(String(60))
    resolved: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(20), default="profile")  # seat / profile
    day: Mapped[date] = mapped_column(Date, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RoomBooking(Base):
    __tablename__ = "room_bookings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.id"), index=True)
    organizer_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    day: Mapped[date] = mapped_column(Date, index=True)
    start: Mapped[str] = mapped_column(String(5))
    end: Mapped[str] = mapped_column(String(5))
    title: Mapped[str] = mapped_column(String(200))


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor_id: Mapped[str] = mapped_column(String(40), index=True)
    action: Mapped[str] = mapped_column(String(50))
    target: Mapped[str] = mapped_column(String(100))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
