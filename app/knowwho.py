"""会いに行ける Know-Who：詳しい人を「見つける」→「話す」→「残す」をつなぐ業務ロジック。

FR-01〜04：推薦（理由・相談適性・接点・出社予定）
FR-05〜11：相談の依頼・受諾・近くの席・当日・メモとナレッジ還元
"""
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from math import hypot
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import AuditLog, Consultation, ConsultationMemo, ConsultationRequest, Expertise, Person, Presence, Reservation
from .seed_data import STATUSES, match_topics
from .services import Ctx, dist_label, floor_state, presences
from .timeutil import aware, today

LIMIT = 5             # 推薦は最大5名（FR-01）
NEAR_RADIUS = 640     # 近くの席は相手から半径10席以内（1席 ≒ 64）（FR-08）
BUSY_MONTH = 5        # 月にこれ以上受けている人は「相談が多め」と出す（R-1）
REMIND_HOURS = 48     # 返事がないとリマインドを出す（FR-06）
DECLINE_REASONS = ["今は忙しい（負担が大きい）", "このテーマに詳しくない", "日時が合わない", "その他"]
BURDEN = DECLINE_REASONS[0]


def weekdays_from(start: date, n: int = 5) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def end_hhmm(start: str, minutes: int) -> str:
    h, m = map(int, start.split(":"))
    t = h * 60 + m + minutes
    return f"{t // 60:02d}:{t % 60:02d}"


# ---------- 出社予定（FR-04） ----------
class Schedules:
    """これから数日の出社予定を、まとめて読んでおく。"""

    def __init__(self, db: Session, ctx: Ctx):
        self.ctx = ctx
        self.days = weekdays_from(today())
        self.pres = presences(db, today())
        self.rsv: dict[str, dict[date, Reservation]] = defaultdict(dict)
        for r in db.scalars(select(Reservation).where(Reservation.day >= self.days[0], Reservation.day <= self.days[-1])):
            self.rsv[r.person_id][r.day] = r

    def day_of(self, pid: str, d: date) -> dict:
        p = self.pres.get(pid) if d == today() else None
        if p and p.status == "office" and p.seat_id:
            s = self.ctx.seats[p.seat_id]
            return {"day": d.isoformat(), "status": "office", "label": "出社", "floor": s.floor_id, "area": s.area}
        if p and p.status in STATUSES and p.status != "office":
            return {"day": d.isoformat(), "status": p.status, "label": STATUSES[p.status], "floor": None, "area": None}
        r = self.rsv[pid].get(d)
        if r:  # 座席番号は出さず、フロアとエリアだけ（FR-03・04）
            s = self.ctx.seats[r.seat_id]
            return {"day": d.isoformat(), "status": "office", "label": "出社予定", "floor": s.floor_id, "area": s.area}
        return {"day": d.isoformat(), "status": "none", "label": "予定なし", "floor": None, "area": None}

    def week(self, pid: str) -> dict:
        if not self.ctx.can_see_schedule(pid):
            return {"visible": False, "days": [], "next_office": None}
        days = [self.day_of(pid, d) for d in self.days]
        nxt = next((x["day"] for x in days if x["status"] == "office"), None)
        return {"visible": True, "days": days, "next_office": nxt}


# ---------- 接点（FR-03） ----------
def _rel(ctx: Ctx, a: str, x: str, a_label: str) -> str | None:
    pa, px = ctx.people[a], ctx.people[x]
    if ctx.links[a].get(x):
        return f"{a_label}と相談したことがある"
    if pa.dept_id == px.dept_id:
        return f"{a_label}と同じ部署"
    return None


def contacts(ctx: Ctx, me: str, other: str, limit: int = 3) -> dict:
    """自分と相手の両方につながりがある人（共通の知人）と、直接の接点。"""
    name = ctx.people[other].name
    common = []
    for x in ctx.people:
        if x in (me, other):
            continue
        r1, r2 = _rel(ctx, me, x, "あなた"), _rel(ctx, other, x, f"{name}さん")
        if r1 and r2:
            strength = ctx.links[me].get(x, 0) + ctx.links[other].get(x, 0)
            common.append((strength, x, [r1, r2]))
    common.sort(key=lambda t: (-t[0], t[1]))
    direct = []
    if ctx.links[me].get(other):
        direct.append(f"以前に{ctx.links[me][other]}回、相談でつながっています")
    if ctx.people[me].dept_id == ctx.people[other].dept_id:
        direct.append("同じ部署です")
    return {"common": [{"person": ctx.person(x), "kinds": kinds} for _, x, kinds in common[:limit]], "common_total": len(common), "direct": direct}


# ---------- 推薦（FR-01・02） ----------
def month_load(db: Session) -> Counter:
    since = datetime.combine(today().replace(day=1), time(), ZoneInfo(settings.timezone))
    rows = db.scalars(select(ConsultationRequest).where(ConsultationRequest.status.in_(("accepted", "done"))))
    return Counter(r.expert_id for r in rows if r.responded_at and aware(r.responded_at) >= since)


def suitability(ctx: Ctx, pid: str, topics: list[str], load: Counter) -> dict:
    sk = [ctx.skills[pid][t] for t in topics if t in ctx.skills.get(pid, {})]
    n = load.get(pid, 0)
    return {"docs": sum(e.docs for e in sk), "answers": sum(e.answers for e in sk), "helped": sum(ctx.helped.get((pid, e.topic), 0) for e in sk),
            "month": n, "accepting": ctx.pref(pid)["opt_in"], "busy": n >= BUSY_MONTH}


def reason(ctx: Ctx, pid: str, topics: list[str]) -> str:
    """推薦理由を必ず1文で（FR-01・NF-10）。"""
    best = max((ctx.skills[pid][t] for t in topics if t in ctx.skills.get(pid, {})),
               key=lambda e: ctx.helped.get((pid, e.topic), 0) * 2 + e.answers + e.docs)
    helped = ctx.helped.get((pid, best.topic), 0)
    if best.learned:
        return f"「{best.topic}」の相談に乗って解決した記録があり、相談から聞けることに加わりました。"
    if helped:
        return f"「{best.topic}」で{helped}回頼られ、関わった資料{best.docs}件・回答{best.answers}件の実績があります。"
    return f"「{best.topic}」の資料{best.docs}件に関わり、質問に{best.answers}回答えています。"


def candidates(db: Session, ctx: Ctx, topics: list[str], me: Person, exclude: set[str] = frozenset(), limit: int = LIMIT,
               sched: Schedules | None = None) -> list[dict]:
    sched = sched or Schedules(db, ctx)
    load = month_load(db)
    out = []
    for pid in ctx.people:
        if pid == me.id or pid in exclude or not ctx.pref(pid)["opt_in"]:
            continue
        matched = [t for t in topics if t in ctx.skills.get(pid, {})]
        if not matched:
            continue
        week = sched.week(pid)
        con = contacts(ctx, me.id, pid)
        suit = suitability(ctx, pid, matched, load)
        idx = next((i for i, d in enumerate(week["days"]) if d["status"] == "office"), None)
        near_day = 6 if idx == 0 else 4 if idx is not None and idx <= 2 else 2 if idx is not None else 0
        # 並び順：分野の一致・回答の実績・接点の強さ・出社予定の近さ（FR-01）。忙しい人は少し下げて負担を散らす（R-1）
        score = (10 * len(matched) + suit["answers"] + 2 * suit["helped"] + 3 * min(3, len(con["common"]))
                 + (4 if ctx.links[me.id].get(pid) else 0) + near_day - (6 if suit["busy"] else 0))
        out.append({"person": ctx.person(pid), "topics": matched, "reason": reason(ctx, pid, matched), "suitability": suit,
                    "contacts": con, "schedule": week, "score": score})
    out.sort(key=lambda e: (-e["score"], e["person"]["id"]))
    return out[:limit]


# ---------- 過去の相談（ナレッジ）（FR-11・巡る） ----------
def knowledge_view(ctx: Ctx, m: ConsultationMemo, r: ConsultationRequest) -> dict:
    return {"id": m.id, "topic": r.topic, "viewpoint": r.viewpoint, "expert": ctx.person(r.expert_id), "day": (r.day or r.created_at.date()).isoformat(),
            "heard": m.heard, "learned": m.learned, "reuse": m.reuse, "views": m.views}


def knowledge(db: Session, ctx: Ctx, topics: list[str] | None = None, expert_id: str | None = None, limit: int = 5) -> list[dict]:
    q = (select(ConsultationMemo, ConsultationRequest).join(ConsultationRequest, ConsultationMemo.request_id == ConsultationRequest.id)
         .where(ConsultationMemo.status == "approved").order_by(ConsultationMemo.approved_at.desc()))
    if topics is not None:
        q = q.where(ConsultationRequest.topic.in_(topics))
    if expert_id:
        q = q.where(ConsultationRequest.expert_id == expert_id)
    return [knowledge_view(ctx, m, r) for m, r in db.execute(q.limit(limit))]


def search(db: Session, ctx: Ctx, q: str, me: Person) -> dict:
    topics = match_topics(q)
    if not topics:
        return {"q": q, "topics": [], "candidates": [], "knowledge": []}
    return {"q": q, "topics": topics, "candidates": candidates(db, ctx, topics, me), "knowledge": knowledge(db, ctx, topics)}


def profile(db: Session, ctx: Ctx, pid: str, me: Person) -> dict:
    sched = Schedules(db, ctx)
    load = month_load(db)
    topics = list(ctx.skills.get(pid, {}))
    p = ctx.pref(pid)
    mine = db.scalars(select(ConsultationRequest).where(ConsultationRequest.asker_id == me.id, ConsultationRequest.expert_id == pid)
                      .order_by(ConsultationRequest.created_at.desc()))
    return {"person": ctx.person(pid), "schedule": sched.week(pid), "contacts": contacts(ctx, me.id, pid) if pid != me.id else None,
            "suitability": suitability(ctx, pid, topics, load), "opt_in": p["opt_in"],
            "reason": reason(ctx, pid, topics) if topics else None,
            "knowledge": knowledge(db, ctx, expert_id=pid), "my_requests": [{"id": r.id, "topic": r.topic, "status": r.status} for r in mine]}


# ---------- 相談の依頼（FR-05〜11） ----------
def find_seat_of(db: Session, ctx: Ctx, pid: str, d: date) -> dict | None:
    """その日にその人が座る席（今日は在席、先の日付は予約）。"""
    if d == today():
        p = db.scalar(select(Presence).where(Presence.person_id == pid, Presence.day == d))
        if p and p.status == "office" and p.seat_id and not p.checked_out_at:
            return ctx.seat(p.seat_id)
    r = db.scalar(select(Reservation).where(Reservation.person_id == pid, Reservation.day == d))
    return ctx.seat(r.seat_id) if r else None


def nearby_seats(db: Session, ctx: Ctx, me: Person, around: dict, d: date, limit: int = 3) -> list[dict]:
    st = floor_state(db, ctx, around["floor"], d, me)
    free = [ctx.seats[x["id"]] for x in st["seats"] if not x["occ"]]
    near = sorted(((hypot(f.x - around["x"], f.y - around["y"]), f) for f in free), key=lambda t: (t[0], t[1].id))
    return [{**ctx.seat(f.id), "dist": round(dd), "dist_label": dist_label(dd)} for dd, f in near if dd <= NEAR_RADIUS][:limit]


def step_of(r: ConsultationRequest, memo: ConsultationMemo | None) -> int:
    """依頼 1 → 受諾 2 → 席の確保 3 → 当日 4 → メモ 5 → 残った 6"""
    if memo and memo.status == "approved":
        return 6
    if r.status == "done" or r.started_at:
        return 5
    if r.status == "accepted":
        return 4 if r.reservation_id or r.mode == "online" else 3
    return 1 if r.status == "pending" else 0


def request_view(db: Session, ctx: Ctx, r: ConsultationRequest, me: Person, full: bool = True) -> dict:
    role = "asker" if r.asker_id == me.id else "expert"
    memo = db.scalar(select(ConsultationMemo).where(ConsultationMemo.request_id == r.id))
    now = datetime.now(timezone.utc)
    created = aware(r.created_at)
    started = aware(r.started_at)
    v = {"id": r.id, "role": role, "status": r.status, "topic": r.topic, "purpose": r.purpose, "viewpoint": r.viewpoint, "message": r.message,
         "slots": r.slots, "duration": r.duration, "mode": r.mode, "day": r.day.isoformat() if r.day else None, "start": r.start,
         "end": end_hhmm(r.start, r.duration) if r.start else None, "asker": ctx.person(r.asker_id), "expert": ctx.person(r.expert_id),
         "decline_reason": r.decline_reason, "created_at": created.isoformat(),
         "started_at": started.isoformat() if started else None,
         "ends_at": (started + timedelta(minutes=r.duration)).isoformat() if started else None,
         "overdue": r.status == "pending" and now - created > timedelta(hours=REMIND_HOURS),
         "is_today": r.day == today(), "step": step_of(r, memo), "helpful": r.helpful,
         "memo": {"heard": memo.heard, "learned": memo.learned, "reuse": memo.reuse, "status": memo.status, "author": ctx.person(memo.author_id),
                  "approved_at": aware(memo.approved_at).isoformat() if memo.approved_at else None} if memo else None}
    if not full:
        return v
    # 希望日時ごとに、相手がその日どこにいる予定か（受諾者が選びやすいように）
    sched = Schedules(db, ctx)
    slot_info = []
    for s in r.slots:
        d = date.fromisoformat(s["day"])
        info = sched.day_of(r.expert_id, d) if d in sched.days else {"status": "none", "label": "予定なし"}
        slot_info.append({**s, "end": end_hhmm(s["start"], r.duration), "expert": {"status": info["status"], "label": info["label"], "area": info.get("area"), "floor": info.get("floor")},
                          "past": d < today()})
    v["slots"] = slot_info
    if r.status == "accepted" and r.day:
        v["expert_seat"] = find_seat_of(db, ctx, r.expert_id, r.day)
        v["asker_seat"] = find_seat_of(db, ctx, r.asker_id, r.day)
        if role == "asker" and r.mode == "office" and not v["asker_seat"] and v["expert_seat"] and r.day >= today():
            v["nearby"] = nearby_seats(db, ctx, me, v["expert_seat"], r.day)
    if r.status == "declined" and role == "asker":
        v["alternatives"] = candidates(db, ctx, [r.topic], me, exclude={r.expert_id}, limit=2)
    return v


def record_consultation(db: Session, r: ConsultationRequest) -> None:
    """相談が実施されたら、Know-Who の「頼られた」に数える。"""
    d = r.day or today()
    if not db.scalar(select(Consultation.id).where(Consultation.asker_id == r.asker_id, Consultation.expert_id == r.expert_id,
                                                   Consultation.day == d, Consultation.topic == r.topic)):
        db.add(Consultation(asker_id=r.asker_id, expert_id=r.expert_id, topic=r.topic, resolved=r.helpful is not False, source="request", day=d))


def learn_topic(db: Session, r: ConsultationRequest) -> bool:
    """承認された相談の分野は、相手の「聞けること」に加わる。"""
    if db.scalar(select(Expertise.id).where(Expertise.person_id == r.expert_id, Expertise.topic == r.topic)):
        return False
    db.add(Expertise(person_id=r.expert_id, topic=r.topic, learned=True))
    return True


def inbox(db: Session, ctx: Ctx, me: Person) -> dict:
    """対応が必要なもの（画面の通知バッジ）。"""
    rows = list(db.scalars(select(ConsultationRequest).where((ConsultationRequest.asker_id == me.id) | (ConsultationRequest.expert_id == me.id))))
    memos = {m.request_id: m for m in db.scalars(select(ConsultationMemo).where(ConsultationMemo.request_id.in_([r.id for r in rows] or [-1])))}
    todo = []
    for r in rows:
        m = memos.get(r.id)
        if r.expert_id == me.id and r.status == "pending":
            todo.append(("respond", r.id))
        elif r.asker_id == me.id and r.status == "accepted" and r.mode == "office" and not r.reservation_id and r.day and r.day >= today():
            todo.append(("seat", r.id))
        elif r.asker_id == me.id and (r.status == "done" or r.started_at) and not m:
            todo.append(("memo", r.id))
        elif r.expert_id == me.id and m and m.status == "pending":
            todo.append(("approve", r.id))
        elif r.status == "accepted" and r.day == today() and not r.started_at:
            todo.append(("today", r.id))
    return {"count": len(todo), "items": [{"kind": k, "id": i} for k, i in todo]}


def cycle(db: Session, days: int) -> dict:
    """検証する仮説とKPI（要件定義書 5.2）。座席の利用率ではなく、相談が生まれ、知が残ったかを見る。"""
    since = datetime.combine(today() - timedelta(days=days), time(), ZoneInfo(settings.timezone))
    reqs = [r for r in db.scalars(select(ConsultationRequest)) if aware(r.created_at) >= since]
    searches = sum(1 for a in db.scalars(select(AuditLog).where(AuditLog.action == "knowwho.search")) if aware(a.at) >= since)
    ids = [r.id for r in reqs] or [-1]
    memos = list(db.scalars(select(ConsultationMemo).where(ConsultationMemo.request_id.in_(ids))))
    approved = [m for m in memos if m.status == "approved"]
    done = [r for r in reqs if r.status == "done"]
    # まだ先の相談（依頼中・受諾済みで当日前）は、完了率の分母に入れない
    settled = [r for r in reqs if r.status in ("done", "declined", "cancelled") or (r.status == "accepted" and r.day and r.day < today())]
    answered = [r for r in reqs if r.status != "pending" and r.responded_at]
    accepted = [r for r in answered if r.status in ("accepted", "done")]
    declined = [r for r in reqs if r.status == "declined"]
    rated = [r for r in reqs if r.helpful is not None]
    pct = lambda a, b: round(100 * a / b, 1) if b else None  # noqa: E731
    return {
        "requests": len(reqs), "searches": searches, "done": len(done), "approved": len(approved),
        "h1_request_rate": pct(len(reqs), searches),
        "h2_completion": pct(len(done), len(settled)),
        "h3_helpful": pct(sum(1 for r in rated if r.helpful), len(rated)),
        "h4_knowledge": pct(len(approved), len(done)),
        "h5_reference": pct(sum(1 for m in approved if m.views), len(approved)),
        "h6_accept": pct(len(accepted), len(answered)),
        "h6_burden": pct(sum(1 for r in declined if r.decline_reason == BURDEN), len(declined)),
        "targets": {"h1": 30, "h2": 60, "h3": 70, "h4": 40, "h5": 20, "h6": 50, "h6_burden": 20},
        "decline_reasons": [{"reason": k, "n": n} for k, n in Counter(r.decline_reason or "理由なし" for r in declined).most_common()],
    }
