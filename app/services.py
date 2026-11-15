"""業務ロジック。APIの形に依存しない計算をここに置く。"""
from collections import Counter, defaultdict
from datetime import date, timedelta
from math import hypot

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import AuditLog, Consultation, Department, Expertise, Floor, Person, Presence, Reservation, Room, RoomBooking, Seat
from .seed_data import STATUSES, match_topics
from .timeutil import aware, hhmm_now, local_hhmm, today

KIND_LABEL = {"desk": "フリーアドレス", "booth": "集中ブース", "lounge": "ラウンジ"}


def audit(db: Session, actor: Person, action: str, target: str, **detail) -> None:
    db.add(AuditLog(actor_id=actor.id, action=action, target=target, detail=detail))


def parse_day(s: str | None) -> date:
    if not s:
        return today()
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, "日付は YYYY-MM-DD で指定してください")


def overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return a_start < b_end and b_start < a_end


class Ctx:
    """1リクエストの中で何度も使う参照データをまとめて読む。"""

    def __init__(self, db: Session):
        self.db = db
        self.depts = {d.id: d.name for d in db.scalars(select(Department))}
        self.people = {p.id: p for p in db.scalars(select(Person))}
        self.seats = {s.id: s for s in db.scalars(select(Seat))}
        self.floors = {f.id: f for f in db.scalars(select(Floor))}
        self.skills: dict[str, dict[str, Expertise]] = defaultdict(dict)
        for e in db.scalars(select(Expertise)):
            self.skills[e.person_id][e.topic] = e
        self.helped = Counter((c.expert_id, c.topic) for c in db.scalars(select(Consultation).where(Consultation.resolved.is_(True))))

    def skill_list(self, pid: str) -> list[dict]:
        out = [{"topic": t, "docs": e.docs, "answers": e.answers, "helped": self.helped.get((pid, t), 0), "learned": e.learned}
               for t, e in self.skills.get(pid, {}).items()]
        return sorted(out, key=lambda x: -(x["helped"] * 2 + x["answers"] + x["docs"]))

    def person(self, pid: str) -> dict:
        p = self.people[pid]
        return {"id": p.id, "name": p.name, "dept": p.dept_id, "dept_name": self.depts.get(p.dept_id, ""), "title": p.title,
                "color": p.color, "email": p.email, "is_admin": p.is_admin, "skills": self.skill_list(pid)}

    def seat(self, sid: str) -> dict:
        s = self.seats[sid]
        return {"id": s.id, "name": s.name, "area": s.area, "kind": s.kind, "kind_label": KIND_LABEL.get(s.kind, s.kind),
                "features": s.features, "floor": s.floor_id, "floor_name": self.floors[s.floor_id].name, "x": s.x, "y": s.y}


def presences(db: Session, day: date) -> dict[str, Presence]:
    return {p.person_id: p for p in db.scalars(select(Presence).where(Presence.day == day))}


def reservations_on(db: Session, day: date) -> list[Reservation]:
    return list(db.scalars(select(Reservation).where(Reservation.day == day)))


def location(ctx: Ctx, pid: str, pres: dict, rsv: list[Reservation], day: date) -> dict:
    """その日にその人がどこにいるか（今日は在席、先の日付は予約）。"""
    p = pres.get(pid)
    if p and p.status == "office" and p.seat_id and not p.checked_out_at:
        return {"status": "office", "label": STATUSES["office"], "seat": ctx.seat(p.seat_id), "since": local_hhmm(p.checked_in_at)}
    if p and p.status in STATUSES and p.status != "office":
        return {"status": p.status, "label": STATUSES[p.status], "seat": None}
    r = next((r for r in rsv if r.person_id == pid), None)
    if r:
        return {"status": "reserved", "label": "予約あり", "seat": ctx.seat(r.seat_id), "start": r.start, "end": r.end, "all_day": r.all_day}
    if p and p.checked_out_at:
        return {"status": "left", "label": "退社", "seat": None}
    return {"status": "none", "label": "未設定" if day == today() else "予定なし", "seat": None}


def floor_state(db: Session, ctx: Ctx, floor_id: str, day: date, me: Person) -> dict:
    if floor_id not in ctx.floors:
        raise HTTPException(404, "フロアが見つかりません")
    is_today = day == today()
    pres = presences(db, day) if is_today else {}
    rsv = reservations_on(db, day)
    seat_rows = [s for s in ctx.seats.values() if s.floor_id == floor_id and s.active]
    by_seat_in = {p.seat_id: p for p in pres.values() if p.status == "office" and p.seat_id and not p.checked_out_at}
    by_seat_rsv = defaultdict(list)
    for r in rsv:
        by_seat_rsv[r.seat_id].append(r)
    now = hhmm_now()
    seats = []
    for s in seat_rows:
        occ = None
        if s.id in by_seat_in:
            p = by_seat_in[s.id]
            occ = {"person": p.person_id, "kind": "in", "since": local_hhmm(p.checked_in_at)}
        elif by_seat_rsv.get(s.id):
            rs = sorted(by_seat_rsv[s.id], key=lambda r: r.start)
            cur = next((r for r in rs if r.all_day or not is_today or r.end > now), None)
            if cur:
                occ = {"person": cur.person_id, "kind": "rsv", "start": cur.start, "end": cur.end, "all_day": cur.all_day, "reservation": cur.id}
        seats.append({"id": s.id, "occ": occ})
    rooms = []
    for r in db.scalars(select(Room).where(Room.floor_id == floor_id)):
        bks = sorted(db.scalars(select(RoomBooking).where(RoomBooking.room_id == r.id, RoomBooking.day == day)), key=lambda b: b.start)
        busy = is_today and any(b.start <= now < b.end for b in bks)
        nxt = next((b.start for b in bks if is_today and b.start > now), None)
        rooms.append({"id": r.id, "busy": busy, "next": nxt, "count": len(bks)})
    zones = {k: [] for k in STATUSES if k != "office"}
    if is_today:
        for p in pres.values():
            if p.status in zones:
                zones[p.status].append(p.person_id)
    used = sum(1 for s in seats if s["occ"])
    my = location(ctx, me.id, presences(db, day) if is_today else {}, rsv, day)
    my_rsv = next((r for r in rsv if r.person_id == me.id), None)
    return {"day": day.isoformat(), "is_today": is_today, "floor": floor_id, "seats": seats, "rooms": rooms, "zones": zones,
            "occupancy": {"used": used, "total": len(seats)},
            "me": {"location": my, "reservation": reservation_view(ctx, my_rsv) if my_rsv else None, "followup": followup(db, ctx, me, day, pres, my_rsv)}}


def followup(db: Session, ctx: Ctx, me: Person, day: date, pres: dict, my_rsv: Reservation | None) -> dict | None:
    """詳しい人の近くを選んで来た日に「聞けましたか？」と1回だけ聞く。"""
    if day != today():
        return None
    p = pres.get(me.id)
    src = p if p and p.status == "office" and p.near_person_id else my_rsv if my_rsv and my_rsv.near_person_id else None
    if not src or src.near_person_id not in ctx.people:
        return None
    done = db.scalar(select(Consultation.id).where(Consultation.asker_id == me.id, Consultation.expert_id == src.near_person_id, Consultation.day == day))
    if done:
        return None
    return {"person": ctx.person(src.near_person_id), "purpose": src.purpose or ""}


def dist_label(d: float) -> str:
    return "すぐとなり" if d <= 75 else f"{max(2, round(d / 64))}席ほど先"


def recommend(db: Session, ctx: Ctx, q: str, day: date, me: Person) -> dict:
    """今日やることに詳しい人と、その人の近くの空席。"""
    topics = match_topics(q)
    out = {"q": q, "topics": topics, "here": [], "later": [], "elsewhere": []}
    if not topics:
        return out
    is_today = day == today()
    pres = presences(db, day) if is_today else {}
    rsv = reservations_on(db, day)
    states: dict[str, dict] = {}
    for pid in ctx.people:
        if pid == me.id:
            continue
        matched = [t for t in topics if t in ctx.skills.get(pid, {})]
        if not matched:
            continue
        loc = location(ctx, pid, pres, rsv, day)
        strength = sum(ctx.helped.get((pid, t), 0) * 2 + ctx.skills[pid][t].answers for t in matched)
        entry = {"person": ctx.person(pid), "topics": matched, "location": loc, "strength": strength}
        if loc.get("seat") and loc["status"] in ("office", "reserved"):
            fid = loc["seat"]["floor"]
            if fid not in states:
                states[fid] = floor_state(db, ctx, fid, day, me)
            sx, sy = loc["seat"]["x"], loc["seat"]["y"]
            free = [ctx.seats[x["id"]] for x in states[fid]["seats"] if not x["occ"]]
            near = sorted(((hypot(f.x - sx, f.y - sy), f) for f in free), key=lambda t: t[0])[:3]
            entry["seats"] = [{**ctx.seat(f.id), "dist": round(d), "dist_label": dist_label(d)} for d, f in near if d < 280]
            out["here"].append(entry)
        else:
            nxt = db.scalar(select(Reservation).where(Reservation.person_id == pid, Reservation.day > day,
                                                      Reservation.day <= today() + timedelta(days=settings.book_ahead_days)).order_by(Reservation.day))
            if nxt:
                entry["next"] = {"day": nxt.day.isoformat(), "seat": ctx.seat(nxt.seat_id)}
                out["later"].append(entry)
            else:
                out["elsewhere"].append(entry)
    for k in ("here", "later", "elsewhere"):
        out[k].sort(key=lambda e: -e["strength"])
    out["elsewhere"] = out["elsewhere"][:5]
    return out


def reservation_view(ctx: Ctx, r: Reservation) -> dict:
    return {"id": r.id, "seat": ctx.seat(r.seat_id), "person": ctx.person(r.person_id), "day": r.day.isoformat(), "start": r.start, "end": r.end, "all_day": r.all_day,
            "purpose": r.purpose, "near": ctx.person(r.near_person_id) if r.near_person_id in ctx.people else None}


def seat_detail(db: Session, ctx: Ctx, sid: str, day: date) -> dict:
    if sid not in ctx.seats:
        raise HTTPException(404, "座席が見つかりません")
    rsv = sorted((r for r in reservations_on(db, day) if r.seat_id == sid), key=lambda r: r.start)
    occupant = None
    if day == today():
        p = db.scalar(select(Presence).where(Presence.day == day, Presence.seat_id == sid, Presence.status == "office", Presence.checked_out_at.is_(None)))
        if p:
            occupant = {"person": ctx.person(p.person_id), "since": local_hhmm(p.checked_in_at)}
    return {"seat": ctx.seat(sid), "day": day.isoformat(), "occupant": occupant, "reservations": [reservation_view(ctx, r) for r in rsv]}


def check_booking_window(day: date, start: str, end: str, all_day: bool) -> tuple[str, str]:
    if day < today():
        raise HTTPException(422, "過去の日付は予約できません")
    if day > today() + timedelta(days=settings.book_ahead_days):
        raise HTTPException(422, f"予約できるのは{settings.book_ahead_days}日先までです")
    if all_day:
        return "09:00", "18:00"
    lo, hi = f"{settings.open_hour:02d}:00", f"{settings.close_hour:02d}:00"
    if not (lo <= start < end <= hi):
        raise HTTPException(422, f"時間は{lo}〜{hi}の範囲で、終了を開始より後にしてください")
    return start, end


def analytics(db: Session, ctx: Ctx, days: int) -> dict:
    t = today()
    start = t - timedelta(days=days)
    rows = list(db.scalars(select(Presence).where(Presence.day >= start, Presence.day <= t)))
    total_seats = sum(1 for s in ctx.seats.values() if s.active)
    office = [p for p in rows if p.status == "office" and p.seat_id]
    by_day = Counter(p.day for p in office)
    weekdays = sorted({d for d in (start + timedelta(days=i) for i in range(days + 1)) if d.weekday() < 5})
    daily = [{"day": d.isoformat(), "rate": round(100 * by_day.get(d, 0) / total_seats, 1), "count": by_day.get(d, 0)} for d in weekdays]
    wd = defaultdict(list)
    for d in daily:
        wd[date.fromisoformat(d["day"]).weekday()].append(d["rate"])
    weekday = [{"wd": "月火水木金"[i], "rate": round(sum(wd[i]) / len(wd[i]), 1) if wd[i] else 0} for i in range(5)]
    area_seats = Counter(s.area for s in ctx.seats.values() if s.active)
    area_use = Counter(ctx.seats[p.seat_id].area for p in office if p.day < t)
    past_days = max(1, len([d for d in weekdays if d < t]))
    areas = sorted([{"area": a, "rate": round(100 * area_use.get(a, 0) / (n * past_days), 1), "seats": n} for a, n in area_seats.items()], key=lambda x: -x["rate"])
    hours = Counter()
    for p in office:
        hm = local_hhmm(aware(p.checked_in_at))
        if hm:
            hours[int(hm[:2])] += 1
    status_mix = Counter(p.status for p in rows if p.day < t)
    rsv_past = list(db.scalars(select(Reservation).where(Reservation.day >= start, Reservation.day < t)))
    showed = {(p.person_id, p.day) for p in office}
    no_show = sum(1 for r in rsv_past if (r.person_id, r.day) not in showed)
    past_rates = [d["rate"] for d in daily if d["day"] < t.isoformat()]
    today_rate = round(100 * by_day.get(t, 0) / total_seats, 1)
    cons = list(db.scalars(select(Consultation).where(Consultation.day >= start, Consultation.day <= t)))
    dept_of = {pid: p.dept_id for pid, p in ctx.people.items()}
    cross = [c for c in cons if dept_of.get(c.asker_id) != dept_of.get(c.expert_id)]
    monday = t - timedelta(days=t.weekday())
    weeks = []
    for w in range(3, -1, -1):
        ws = monday - timedelta(days=7 * w)
        wc = [c for c in cons if ws <= c.day < ws + timedelta(days=7)]
        weeks.append({"week": ws.isoformat(), "total": len(wc), "seat": sum(1 for c in wc if c.source == "seat")})
    flows = Counter((ctx.depts.get(dept_of[c.asker_id], ""), ctx.depts.get(dept_of[c.expert_id], "")) for c in cross)
    knowwho = {
        "total": len(cons), "seat": sum(1 for c in cons if c.source == "seat"),
        "cross_rate": round(100 * len(cross) / len(cons), 1) if cons else 0,
        "resolved_rate": round(100 * sum(1 for c in cons if c.resolved) / len(cons), 1) if cons else 0,
        "experts": len({c.expert_id for c in cons if c.resolved}),
        "learned": sum(1 for d in ctx.skills.values() for e in d.values() if e.learned),
        "weeks": weeks,
        "flows": [{"from": a, "to": b, "n": n} for (a, b), n in flows.most_common(5)],
        "topics": [{"topic": k, "n": n} for k, n in Counter(c.topic for c in cons).most_common(6)],
    }
    return {
        "knowwho": knowwho,
        "kpi": {"today": today_rate, "avg": round(sum(past_rates) / len(past_rates), 1) if past_rates else 0,
                "peak_hour": f"{max(hours, key=hours.get):02d}時台" if hours else "-",
                "no_show": round(100 * no_show / len(rsv_past), 1) if rsv_past else 0, "seats": total_seats},
        "daily": daily, "weekday": weekday, "areas": areas,
        "checkin_hours": [{"h": h, "n": hours.get(h, 0)} for h in range(7, 13)],
        "status_mix": [{"status": k, "label": STATUSES[k], "n": status_mix.get(k, 0)} for k in STATUSES],
    }
