"""APIの結合テスト。SQLiteの一時ファイル（または TEST_DATABASE_URL の MySQL）で、
在席・予約・会議室・権限の決まりごとを確かめる。

実行：cd backend → pytest -q
MySQL：TEST_DATABASE_URL=mysql+pymysql://user:pass@127.0.0.1:3306/seats_test?charset=utf8mb4 DB_SSL=disable pytest -q
"""
import importlib
import os
import sys
import tempfile
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def make_client(auth_mode: str):
    url = os.getenv("TEST_DATABASE_URL") or f"sqlite:///{tempfile.mktemp(suffix='.db')}"
    os.environ.update(DATABASE_URL=url, AUTH_MODE=auth_mode, SEED_ON_START="true")
    for name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
        del sys.modules[name]
    main = importlib.import_module("app.main")
    if not url.startswith("sqlite"):  # MySQL は毎回テーブルを作り直す
        from app.db import Base, engine
        Base.metadata.drop_all(engine)
    return TestClient(main.app)


@pytest.fixture()
def c():
    with make_client("demo") as client:
        yield client


def as_(uid):
    return {"X-Demo-User": uid}


def today(c):
    return date.fromisoformat(c.get("/api/me").json()["today"])


def free_seats(c, day, floor="12F", kind="-D"):
    st = c.get(f"/api/state?floor={floor}&day={day}", headers=as_("u00")).json()
    return [s["id"] for s in st["seats"] if s["occ"] is None and kind in s["id"]]


def free_of_reservations(c, day, n=3):
    """その日に予約が1件もない席（予約テストの土台）。"""
    out = []
    for sid in free_seats(c, day):
        if not c.get(f"/api/seats/{sid}?day={day}", headers=as_("u00")).json()["reservations"]:
            out.append(sid)
        if len(out) == n:
            return out
    return out


def rsv(c, uid, seat, day, all_day=True, start="09:00", end="18:00"):
    return c.post("/api/reservations", json={"seat_id": seat, "day": str(day), "all_day": all_day, "start": start, "end": end}, headers=as_(uid))


# ---------- 参照 ----------
def test_health_me_and_floors(c):
    assert c.get("/api/health").json() == {"ok": True}
    me = c.get("/api/me", headers=as_("u00")).json()
    assert me["user"]["name"] == "高木 拓海" and me["user"]["is_admin"] and me["mode"] == "demo"
    fl = c.get("/api/floors", headers=as_("u00")).json()
    assert [f["id"] for f in fl["floors"]] == ["12F", "13F"]
    assert sum(len(f["seats"]) for f in fl["floors"]) == 118
    assert len(fl["departments"]) == 6


def test_state_counts_match(c):
    t = today(c)
    st = c.get(f"/api/state?floor=12F&day={t}", headers=as_("u00")).json()
    used = sum(1 for s in st["seats"] if s["occ"])
    assert st["occupancy"] == {"used": used, "total": len(st["seats"])}
    assert st["is_today"] and set(st["zones"]) == {"remote", "trip", "out", "leave"}
    assert c.get(f"/api/state?floor=99F&day={t}", headers=as_("u00")).status_code == 404
    assert c.get("/api/state?floor=12F&day=2026-13-40", headers=as_("u00")).status_code == 422


def test_search_finds_people_seats_rooms(c):
    r = c.get("/api/search?q=田中", headers=as_("u00")).json()
    assert any(p["name"] == "田中 太郎" for p in r["people"])
    assert c.get("/api/search?q=booth-02", headers=as_("u00")).json()["seats"]
    assert c.get("/api/search?q=mozart", headers=as_("u00")).json()["rooms"][0]["name"] == "Mozart"


# ---------- 在席 ----------
def test_checkin_checkout_and_double_booking(c):
    t = today(c)
    seat = free_of_reservations(c, t, 1)[0]
    r = c.post("/api/checkin", json={"seat_id": seat}, headers=as_("u00"))
    assert r.status_code == 200, r.text
    loc = next(p for p in c.get("/api/people?dept=plan", headers=as_("u00")).json()["people"] if p["id"] == "u00")["location"]
    assert loc["status"] == "office" and loc["seat"]["id"] == seat
    # 同じ席に別の人は入れない
    assert c.post("/api/checkin", json={"seat_id": seat}, headers=as_("u45")).status_code == 409
    # 予約もできない
    assert rsv(c, "u45", seat, t).status_code == 409
    assert c.post("/api/checkout", headers=as_("u00")).status_code == 200
    assert c.post("/api/checkout", headers=as_("u00")).status_code == 409
    # 空いたら入れる
    assert c.post("/api/checkin", json={"seat_id": seat}, headers=as_("u45")).status_code == 200


def test_cannot_checkin_to_seat_reserved_now_by_someone_else(c):
    t = today(c)
    seat = free_of_reservations(c, t, 1)[0]
    assert rsv(c, "u46", seat, t).status_code == 201  # 終日
    r = c.post("/api/checkin", json={"seat_id": seat}, headers=as_("u47"))
    assert r.status_code == 409 and "予約" in r.json()["detail"]
    # 予約した本人は入れる
    assert c.post("/api/checkin", json={"seat_id": seat}, headers=as_("u46")).status_code == 200


def test_status_change(c):
    assert c.post("/api/status", json={"status": "remote"}, headers=as_("u00")).status_code == 200
    me = next(p for p in c.get("/api/people?dept=plan", headers=as_("u00")).json()["people"] if p["id"] == "u00")
    assert me["location"]["status"] == "remote"
    st = c.get(f"/api/state?floor=12F&day={today(c)}", headers=as_("u00")).json()
    assert "u00" in st["zones"]["remote"]
    assert c.post("/api/status", json={"status": "none"}, headers=as_("u00")).status_code == 200
    assert c.post("/api/status", json={"status": "office"}, headers=as_("u00")).status_code == 422


# ---------- 予約 ----------
def test_reservation_rules(c):
    d = today(c) + timedelta(days=3)
    a, b = free_of_reservations(c, d, 2)
    assert rsv(c, "u40", a, d, False, "09:00", "12:00").status_code == 201
    assert rsv(c, "u41", a, d, False, "11:00", "13:00").status_code == 409   # 重なる
    assert rsv(c, "u41", a, d, False, "12:00", "15:00").status_code == 201   # ちょうど続きはOK
    assert rsv(c, "u42", a, d).status_code == 409                           # 終日は時間指定とぶつかる
    assert rsv(c, "u40", b, d).status_code == 409                           # 1人1日1席
    assert rsv(c, "u42", b, today(c) - timedelta(days=1)).status_code == 422  # 過去
    assert rsv(c, "u42", b, today(c) + timedelta(days=15)).status_code == 422  # 14日より先
    assert rsv(c, "u42", b, d, False, "06:00", "08:00").status_code == 422   # 営業時間外
    assert rsv(c, "u42", b, d, False, "15:00", "14:00").status_code == 422   # 終わりが先
    assert rsv(c, "u42", b, d, False, "9:00", "10:00").status_code == 422    # 形式
    assert rsv(c, "u42", "12F-NOPE", d).status_code == 404


def test_update_and_cancel_permissions(c):
    d = today(c) + timedelta(days=2)
    a, b = free_of_reservations(c, d, 2)
    r = rsv(c, "u43", a, d)
    assert r.status_code == 201
    rid = r.json()["id"]
    mine = c.get("/api/reservations/mine", headers=as_("u43")).json()["reservations"]
    assert any(x["id"] == rid for x in mine)
    # 席を変える
    up = c.put(f"/api/reservations/{rid}", json={"seat_id": b, "day": str(d), "all_day": False, "start": "10:00", "end": "16:00"}, headers=as_("u43"))
    assert up.status_code == 200 and up.json()["seat"]["id"] == b and up.json()["start"] == "10:00"
    # ほかの人は変更も取り消しもできない（管理者だけ取り消せる）
    assert c.put(f"/api/reservations/{rid}", json={"seat_id": a, "day": str(d)}, headers=as_("u44")).status_code == 404
    assert c.delete(f"/api/reservations/{rid}", headers=as_("u44")).status_code == 404
    assert c.delete(f"/api/reservations/{rid}", headers=as_("u00")).status_code == 200
    assert c.delete(f"/api/reservations/{rid}", headers=as_("u43")).status_code == 404


def test_future_state_shows_reservation(c):
    d = today(c) + timedelta(days=4)
    seat = free_of_reservations(c, d, 1)[0]
    assert rsv(c, "u39", seat, d, False, "13:00", "17:00").status_code == 201
    st = c.get(f"/api/state?floor=12F&day={d}", headers=as_("u39")).json()
    occ = next(s for s in st["seats"] if s["id"] == seat)["occ"]
    assert occ["kind"] == "rsv" and occ["person"] == "u39" and occ["start"] == "13:00"
    assert st["me"]["reservation"]["seat"]["id"] == seat


# ---------- 会議室 ----------
def test_room_booking(c):
    d = today(c) + timedelta(days=1)
    room = "12F-R-Mozart"
    before = c.get(f"/api/rooms/{room}?day={d}", headers=as_("u00")).json()
    assert all(b["end"] <= "19:00" for b in before["bookings"])
    body = {"day": str(d), "start": "19:00", "end": "20:00", "title": "振り返り"}
    assert c.post(f"/api/rooms/{room}/bookings", json=body, headers=as_("u00")).status_code == 201
    assert c.post(f"/api/rooms/{room}/bookings", json={**body, "start": "19:30", "end": "20:30"}, headers=as_("u01")).status_code == 409
    assert c.post(f"/api/rooms/{room}/bookings", json={**body, "title": ""}, headers=as_("u01")).status_code == 422
    after = c.get(f"/api/rooms/{room}?day={d}", headers=as_("u00")).json()
    assert len(after["bookings"]) == len(before["bookings"]) + 1


# ---------- 権限 ----------
def test_analytics_admin_only(c):
    assert c.get("/api/analytics", headers=as_("u01")).status_code == 403
    a = c.get("/api/analytics", headers=as_("u00")).json()
    assert a["kpi"]["seats"] == 118 and len(a["weekday"]) == 5 and a["daily"]
    assert all(0 <= x["rate"] <= 100 for x in a["areas"])


def test_demo_reset(c):
    t = today(c)
    seat = free_of_reservations(c, t, 1)[0]
    c.post("/api/checkin", json={"seat_id": seat}, headers=as_("u00"))
    assert c.post("/api/admin/reset").status_code == 200
    me = next(p for p in c.get("/api/people?dept=plan", headers=as_("u00")).json()["people"] if p["id"] == "u00")
    assert me["location"]["status"] != "office"


def test_sso_mode_uses_easy_auth_header():
    with make_client("sso") as s:
        assert s.get("/api/me").status_code == 401
        assert s.get("/api/me", headers={"X-Demo-User": "u00"}).status_code == 401   # デモ用ヘッダーは効かない
        r = s.get("/api/me", headers={"X-MS-CLIENT-PRINCIPAL-NAME": "USER01@umikaze-bank.example"})
        assert r.status_code == 200 and r.json()["user"]["id"] == "u01" and r.json()["mode"] == "sso"
        assert s.get("/api/me", headers={"X-MS-CLIENT-PRINCIPAL-NAME": "someone@example.com"}).status_code == 403
        assert s.get("/api/users").status_code == 404
        assert s.post("/api/admin/reset").status_code == 404
        assert s.get("/api/docs").status_code == 404


def test_security_headers_and_static(c):
    r = c.get("/")
    assert r.status_code == 200 and "座席ナビ" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert c.get("/api/health").headers["cache-control"] == "no-store"


# ---------- 今日やること → 詳しい人の近く → 相談の記録 ----------
def test_recommend_finds_experts_and_nearby_free_seats(c):
    r = c.get("/api/recommend?q=非対面サービスの審査について聞きたい", headers=as_("u00")).json()
    assert r["topics"][0] == "非対面サービスの審査"
    names = [e["person"]["name"] for e in r["here"]]
    assert "近藤 恵子" in names and "藤田 誠" in names
    for e in r["here"]:
        assert e["location"]["status"] in ("office", "reserved") and e["seats"]
        assert all(s["dist"] < 280 for s in e["seats"])
    assert any(e["person"]["id"] == "u10" and e.get("next") for e in r["later"])  # 今日はリモート、来る日がわかる
    assert c.get("/api/recommend?q=", headers=as_("u00")).json()["here"] == []


def test_checkin_near_expert_then_followup_and_record(c):
    r = c.get("/api/recommend?q=非対面サービスの審査", headers=as_("u00")).json()
    ex = next(e for e in r["here"] if e["person"]["id"] == "u01")
    seat = ex["seats"][0]["id"]
    before = next(k["helped"] for k in ex["person"]["skills"] if k["topic"] == "非対面サービスの審査")
    ok = c.post("/api/checkin", json={"seat_id": seat, "purpose": "非対面サービスの審査", "near_person_id": "u01"}, headers=as_("u00"))
    assert ok.status_code == 200, ok.text
    st = c.get(f"/api/state?floor={seat[:3]}", headers=as_("u00")).json()
    assert st["me"]["followup"]["person"]["id"] == "u01" and st["me"]["followup"]["purpose"] == "非対面サービスの審査"
    rec = c.post("/api/consultations", json={"expert_id": "u01", "topic": "非対面サービスの審査", "resolved": True, "source": "seat"}, headers=as_("u00"))
    assert rec.status_code == 201 and rec.json()["helped"] == before + 1
    st = c.get(f"/api/state?floor={seat[:3]}", headers=as_("u00")).json()
    assert st["me"]["followup"] is None  # 1回だけ聞く
    kw = c.get("/api/analytics", headers=as_("u00")).json()["knowwho"]
    assert kw["total"] >= 1 and kw["seat"] >= 1 and len(kw["weeks"]) == 4


def test_consultation_adds_new_skill_and_rules(c):
    r = c.post("/api/consultations", json={"expert_id": "u05", "topic": "社内勉強会の運営", "resolved": True}, headers=as_("u00"))
    assert r.status_code == 201 and r.json()["learned"] is True
    p = next(x for x in c.get("/api/people?dept=sales", headers=as_("u00")).json()["people"] if x["id"] == "u05")
    sk = next(k for k in p["skills"] if k["topic"] == "社内勉強会の運営")
    assert sk["learned"] and sk["helped"] == 1
    assert c.post("/api/consultations", json={"expert_id": "u00", "topic": "x"}, headers=as_("u00")).status_code == 422
    assert c.post("/api/consultations", json={"expert_id": "nobody", "topic": "x"}, headers=as_("u00")).status_code == 404
    assert c.post("/api/consultations", json={"expert_id": "u05", "topic": ""}, headers=as_("u00")).status_code == 422
    bad = c.post("/api/checkin", json={"seat_id": free_of_reservations(c, today(c), 1)[0], "near_person_id": "u00"}, headers=as_("u00"))
    assert bad.status_code == 422


def test_reservation_keeps_purpose_and_search_by_topic(c):
    d = today(c) + timedelta(days=5)
    seat = free_of_reservations(c, d, 1)[0]
    r = c.post("/api/reservations", json={"seat_id": seat, "day": str(d), "purpose": "補助金の申請を相談", "near_person_id": "u40"}, headers=as_("u00"))
    assert r.status_code == 201 and r.json()["purpose"] == "補助金の申請を相談" and r.json()["near"]["id"] == "u40"
    s = c.get("/api/search?q=補助金", headers=as_("u00")).json()
    assert s["topics"] == ["補助金申請"] and any(p["id"] == "u40" for p in s["people"])
