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
    assert r.status_code == 200 and "Know-Who" in r.text
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


# ---------- 会いに行ける Know-Who：見つける → 話す → 残す ----------
def request_body(day, expert="u01", **kw):
    body = {"expert_id": expert, "topic": "非対面サービスの審査", "purpose": "eKYCで本人確認をどこまで省けるか知りたい",
            "viewpoint": "コンプラの判断基準", "message": "近藤さん\n相談させてください。", "slots": [{"day": str(day), "start": "20:00"}], "duration": 30}
    return {**body, **kw}


def test_knowwho_recommends_with_reason_contacts_and_schedule(c):
    r = c.get("/api/knowwho?q=非対面サービスの審査について聞きたい", headers=as_("u00")).json()
    assert r["topics"][0] == "非対面サービスの審査"
    assert 1 <= len(r["candidates"]) <= 5
    ids = [e["person"]["id"] for e in r["candidates"]]
    assert {"u01", "u18"} <= set(ids) and "u00" not in ids
    for e in r["candidates"]:
        assert e["reason"].endswith("。")  # 推薦理由を必ず1文で
        assert {"docs", "answers", "helped", "month", "accepting"} <= set(e["suitability"])
        assert len(e["contacts"]["common"]) <= 3
        assert e["schedule"]["visible"] and len(e["schedule"]["days"]) == 5
        assert all("seat" not in d for d in e["schedule"]["days"])  # 出社予定はエリアまで。座席番号は出さない
    assert r["knowledge"] and all(k["topic"] == "非対面サービスの審査" for k in r["knowledge"])
    assert c.get("/api/knowwho?q=", headers=as_("u00")).json()["candidates"] == []


def test_opt_out_hides_from_recommendations(c):
    assert "u18" in [e["person"]["id"] for e in c.get("/api/knowwho?q=非対面サービスの審査", headers=as_("u00")).json()["candidates"]]
    assert c.get("/api/prefs", headers=as_("u18")).json() == {"opt_in": True, "scope": "all", "notify": True, "confirmed": False}
    p = c.put("/api/prefs", json={"opt_in": False, "scope": "all"}, headers=as_("u18")).json()
    assert p["opt_in"] is False and p["confirmed"] is True
    assert "u18" not in [e["person"]["id"] for e in c.get("/api/knowwho?q=非対面サービスの審査", headers=as_("u00")).json()["candidates"]]
    assert all(e["person"]["id"] != "u18" for e in c.get("/api/recommend?q=非対面サービスの審査", headers=as_("u00")).json()["here"])
    assert c.put("/api/prefs", json={"opt_in": True, "scope": "everyone"}, headers=as_("u18")).status_code == 422


def test_private_schedule_is_never_returned_to_others(c):
    d = today(c) + timedelta(days=3)
    seat = free_of_reservations(c, d, 1)[0]
    assert c.post("/api/reservations", json={"seat_id": seat, "day": str(d)}, headers=as_("u40")).status_code == 201  # u40 は出社予定を非公開
    st = c.get(f"/api/state?floor={seat[:3]}&day={d}", headers=as_("u00")).json()
    occ = next(s["occ"] for s in st["seats"] if s["id"] == seat)
    assert occ["person"] == "_hidden" and occ["reservation"] is None
    det = c.get(f"/api/seats/{seat}?day={d}", headers=as_("u00")).json()
    assert det["reservations"][0]["person"]["id"] == "_hidden" and det["reservations"][0]["id"] is None
    loc = next(p for p in c.get(f"/api/people?day={d}", headers=as_("u00")).json()["people"] if p["id"] == "u40")["location"]
    assert loc["status"] == "hidden" and loc["seat"] is None
    assert c.get("/api/people/u40/profile", headers=as_("u00")).json()["schedule"] == {"visible": False, "days": [], "next_office": None}
    assert c.get("/api/people/u40/profile", headers=as_("u40")).json()["schedule"]["visible"]  # 自分には見える
    # u17 は「同じ部署だけ」：開発部の人には見え、経営企画部の人には見えない
    assert c.get("/api/people/u17/profile", headers=as_("u18")).json()["schedule"]["visible"]
    assert not c.get("/api/people/u17/profile", headers=as_("u00")).json()["schedule"]["visible"]


def test_request_rules(c):
    t = today(c)
    assert c.post("/api/requests", json=request_body(t, message="   "), headers=as_("u00")).status_code == 422  # 空欄では送れない
    assert c.post("/api/requests", json=request_body(t, expert="u00"), headers=as_("u00")).status_code == 422
    four = [{"day": str(t + timedelta(days=i)), "start": "10:00"} for i in range(4)]
    assert c.post("/api/requests", json=request_body(t, slots=four), headers=as_("u00")).status_code == 422  # 第3候補まで
    assert c.post("/api/requests", json=request_body(t, duration=45), headers=as_("u00")).status_code == 422
    assert c.post("/api/requests", json=request_body(t - timedelta(days=1)), headers=as_("u00")).status_code == 422
    late = request_body(t, slots=[{"day": str(t), "start": "20:45"}])
    assert c.post("/api/requests", json=late, headers=as_("u00")).status_code == 422  # 終わりが営業時間を過ぎる
    r = c.post("/api/requests", json=request_body(t), headers=as_("u00")).json()
    assert c.get(f"/api/requests/{r['id']}", headers=as_("u05")).status_code == 404  # 当事者だけが見られる
    assert c.post(f"/api/requests/{r['id']}/accept", json={"slot": 0}, headers=as_("u00")).status_code == 403
    assert c.post(f"/api/requests/{r['id']}/start", json={}, headers=as_("u00")).status_code == 409  # 受諾前は始められない


def test_request_accept_seat_start_memo_and_knowledge(c):
    t = today(c)
    before = len(c.get("/api/knowwho?q=非対面サービスの審査", headers=as_("u00")).json()["knowledge"])
    r = c.post("/api/requests", json=request_body(t), headers=as_("u00"))
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    got = c.get("/api/requests", headers=as_("u01")).json()
    assert any(x["id"] == rid for x in got["received"]) and any(i["kind"] == "respond" and i["id"] == rid for i in got["inbox"]["items"])
    v = c.post(f"/api/requests/{rid}/accept", json={"slot": 0}, headers=as_("u01")).json()
    assert v["status"] == "accepted" and v["day"] == str(t) and v["end"] == "20:30"
    assert c.post(f"/api/requests/{rid}/accept", json={"slot": 0}, headers=as_("u01")).status_code == 409
    v = c.get(f"/api/requests/{rid}", headers=as_("u00")).json()
    # 承諾と同時に、相手の近くの空席が依頼者の名前で取れている（相談の時間を含む30分）
    assert v["step"] == 4 and v["expert_seat"] and v["asker_seat"]
    ex, me_seat = v["expert_seat"], v["asker_seat"]
    assert ex["floor"] == me_seat["floor"] and ((ex["x"] - me_seat["x"]) ** 2 + (ex["y"] - me_seat["y"]) ** 2) ** 0.5 <= 640
    mine = c.get("/api/reservations/mine", headers=as_("u00")).json()["reservations"]
    auto = next(x for x in mine if x["seat"]["id"] == me_seat["id"])
    assert auto["near"]["id"] == "u01" and not auto["all_day"] and auto["start"] == "20:00" and auto["end"] == "20:30"
    seat = me_seat["id"]
    v = c.post(f"/api/requests/{rid}/start", json={}, headers=as_("u00")).json()
    assert v["started_at"] and v["ends_at"] and v["step"] == 5
    st = c.get(f"/api/state?floor={seat[:3]}", headers=as_("u00")).json()
    assert st["me"]["location"]["status"] == "office" and st["me"]["location"]["seat"]["id"] == seat  # 相談の開始と一緒にチェックイン
    memo = {"heard": "eKYCでどこまで省けたか", "learned": "上限額と追加確認で例外を認めた", "reuse": "先にリスク管理部と事前協議する", "helpful": True}
    assert c.put(f"/api/requests/{rid}/memo", json={**memo, "learned": " "}, headers=as_("u00")).status_code == 422
    v = c.put(f"/api/requests/{rid}/memo", json=memo, headers=as_("u00")).json()
    assert v["status"] == "done" and v["memo"]["status"] == "pending" and v["helpful"] is True
    # 承認されるまで公開しない
    assert len(c.get("/api/knowwho?q=非対面サービスの審査", headers=as_("u05")).json()["knowledge"]) == before
    assert c.post(f"/api/requests/{rid}/memo/approve", headers=as_("u00")).status_code == 403
    v = c.post(f"/api/requests/{rid}/memo/approve", headers=as_("u01")).json()
    assert v["memo"]["status"] == "approved" and v["step"] == 6
    assert c.put(f"/api/requests/{rid}/memo", json=memo, headers=as_("u00")).status_code == 409
    kn = c.get("/api/knowwho?q=非対面サービスの審査", headers=as_("u05")).json()["knowledge"]
    mid = next(k["id"] for k in kn if k["learned"] == memo["learned"])
    assert c.post(f"/api/knowledge/{mid}/view", headers=as_("u05")).json()["views"] == 1
    assert c.post(f"/api/knowledge/{mid}/view", headers=as_("u00")).json()["views"] == 1  # 当事者が開いても数えない
    cyc = c.get("/api/analytics", headers=as_("u00")).json()["cycle"]
    assert cyc["requests"] >= 1 and cyc["approved"] >= 1 and cyc["h4_knowledge"] is not None


def test_request_decline_offers_alternatives_and_cancel_frees_seat(c):
    t = today(c)
    rid = c.post("/api/requests", json=request_body(t + timedelta(days=1), expert="u18"), headers=as_("u00")).json()["id"]
    assert c.post(f"/api/requests/{rid}/decline", json={"reason": "気分"}, headers=as_("u18")).status_code == 422
    v = c.post(f"/api/requests/{rid}/decline", json={"reason": "今は忙しい（負担が大きい）"}, headers=as_("u18")).json()
    assert v["status"] == "declined"
    v = c.get(f"/api/requests/{rid}", headers=as_("u00")).json()
    alt = [e["person"]["id"] for e in v["alternatives"]]
    assert 1 <= len(alt) <= 2 and "u18" not in alt  # 代わりの詳しい人を1名以上
    rid = c.post("/api/requests", json=request_body(t), headers=as_("u00")).json()["id"]
    c.post(f"/api/requests/{rid}/accept", json={"slot": 0}, headers=as_("u01"))
    seat = c.get(f"/api/requests/{rid}", headers=as_("u00")).json()["asker_seat"]["id"]
    assert c.post(f"/api/requests/{rid}/cancel", headers=as_("u00")).json()["status"] == "cancelled"
    assert not any(x["seat"]["id"] == seat for x in c.get("/api/reservations/mine", headers=as_("u00")).json()["reservations"])
    # 出社日が合わないときはオンラインへ。取った席は空ける
    rid = c.post("/api/requests", json=request_body(t), headers=as_("u00")).json()["id"]
    c.post(f"/api/requests/{rid}/accept", json={"slot": 0}, headers=as_("u01"))
    seat = c.get(f"/api/requests/{rid}", headers=as_("u00")).json()["asker_seat"]["id"]
    v = c.post(f"/api/requests/{rid}/online", headers=as_("u01")).json()
    assert v["mode"] == "online" and v["step"] == 4
    assert not any(x["seat"]["id"] == seat for x in c.get("/api/reservations/mine", headers=as_("u00")).json()["reservations"])


def test_online_start_and_demo_inbox(c):
    t = today(c)
    rid = c.post("/api/requests", json=request_body(t), headers=as_("u00")).json()["id"]
    c.post(f"/api/requests/{rid}/accept", json={"slot": 0, "mode": "online"}, headers=as_("u01"))
    v = c.get(f"/api/requests/{rid}", headers=as_("u00")).json()
    assert v["mode"] == "online" and v["step"] == 4 and "nearby" not in v  # オンラインなら席は取らない
    assert c.post(f"/api/requests/{rid}/start", json={}, headers=as_("u01")).json()["started_at"]
    assert c.post(f"/api/requests/{rid}/finish", json={}, headers=as_("u01")).json()["status"] == "done"
    kinds = [i["kind"] for i in c.get("/api/inbox", headers=as_("u00")).json()["items"]]
    assert kinds.count("respond") == 2 and "approve" in kinds and "memo" in kinds  # デモの利用者がすぐ試せる
    received = c.get("/api/requests", headers=as_("u00")).json()["received"]
    assert any(x["overdue"] for x in received)  # 48時間返事がない依頼



# ---------- 5分相談・候補ゼロの受け皿・一言メモ ----------
def test_quick_five_minute_request(c):
    r = c.post("/api/requests/quick", json={"expert_id": "u10", "topic": "非対面サービスの審査", "q": "eKYCの判断基準"}, headers=as_("u00"))
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["duration"] == 5 and v["purpose"] == "eKYCの判断基準" and 1 <= len(v["slots"]) <= 3
    office = [d["day"] for d in c.get("/api/people/u10/profile", headers=as_("u00")).json()["schedule"]["days"] if d["status"] == "office"]
    assert office and v["slots"][0]["day"] == office[0]  # 相手が出社する日を先に出す
    sug = c.get("/api/requests/quick/slots?expert_id=u10", headers=as_("u00")).json()["slots"]
    assert sug == [{"day": x["day"], "start": x["start"]} for x in v["slots"]]  # 画面に出す候補と、自動で送る候補は同じ
    d = str(today(c) + timedelta(days=2))
    v = c.post("/api/requests/quick", json={"expert_id": "u18", "topic": "セキュリティ審査", "slots": [{"day": d, "start": "15:30"}]}, headers=as_("u00")).json()
    assert [(x["day"], x["start"], x["end"]) for x in v["slots"]] == [(d, "15:30", "15:35")]  # 選んだ日時で送れる
    bad = {"expert_id": "u18", "topic": "x", "slots": [{"day": str(today(c) - timedelta(days=1)), "start": "10:00"}]}
    assert c.post("/api/requests/quick", json=bad, headers=as_("u00")).status_code == 422
    assert c.get("/api/requests/quick/slots?expert_id=nobody", headers=as_("u00")).status_code == 404
    assert c.post("/api/requests/quick", json={"expert_id": "u00", "topic": "x"}, headers=as_("u00")).status_code == 422
    assert c.post("/api/requests/quick", json={"expert_id": "nobody", "topic": "x"}, headers=as_("u00")).status_code == 404


def test_zero_candidates_offer_near_people_question_and_gap(c):
    out = {}
    for _ in range(6):  # 「非対面サービスの審査」に詳しい人を全員、推薦の対象外にする
        r = c.get("/api/knowwho?q=非対面サービスの審査", headers=as_("u00")).json()
        if not r["candidates"]:
            break
        for e in r["candidates"]:
            out[e["person"]["id"]] = e["person"]["name"]
            c.put("/api/prefs", json={"opt_in": False, "scope": "all"}, headers=as_(e["person"]["id"]))
    assert "u18" in out and r["candidates"] == [] and r["near"]
    assert not {n["person"]["id"] for n in r["near"]} & (set(out) | {"u00"})  # 対象外の人と自分は出さない
    assert all(n["why"] and not any(name in n["why"] for name in out.values()) for n in r["near"])  # 対象外の人の名前は出さない
    q = c.post("/api/questions", json={"q": "非対面サービスの審査で本人確認をどこまで省けるか"}, headers=as_("u00")).json()
    assert q["status"] == "open"
    assert c.post("/api/questions", json={"q": "  "}, headers=as_("u00")).status_code == 422
    gaps = c.get("/api/analytics", headers=as_("u00")).json()["cycle"]["gaps"]
    assert gaps["searches"] >= 1 and gaps["open_questions"] == 1 and gaps["top"][0]["q"] == "非対面サービスの審査"
    c.put("/api/prefs", json={"opt_in": True, "scope": "all"}, headers=as_("u18"))  # 詳しい人が現れた
    items = c.get("/api/inbox", headers=as_("u00")).json()["items"]
    found = next(i for i in items if i["kind"] == "found")
    assert found["question"]["found"]["id"] == "u18"
    assert c.post(f"/api/questions/{found['id']}/close", headers=as_("u00")).json()["ok"]
    assert all(i["kind"] != "found" for i in c.get("/api/inbox", headers=as_("u00")).json()["items"])
    assert c.post(f"/api/questions/{found['id']}/close", headers=as_("u05")).status_code == 404


def test_one_line_memo_shows_before_people(c):
    t = today(c)
    rid = c.post("/api/requests", json=request_body(t, slots=[{"day": str(t), "start": "20:00"}]), headers=as_("u00")).json()["id"]
    c.post(f"/api/requests/{rid}/accept", json={"slot": 0, "mode": "online"}, headers=as_("u01"))
    c.post(f"/api/requests/{rid}/start", json={}, headers=as_("u00"))
    v = c.post(f"/api/requests/{rid}/finish", json={"helpful": True, "note": "金額の上限を決めれば、本人確認書類は1点で通った"}, headers=as_("u00")).json()
    assert v["status"] == "done" and v["memo"]["learned"] == "金額の上限を決めれば、本人確認書類は1点で通った" and v["memo"]["status"] == "pending"
    v = c.post(f"/api/requests/{rid}/memo/approve", headers=as_("u01")).json()
    assert v["memo"]["status"] == "approved"
    # テーマに当てはまらない言葉でも、メモの本文が近ければ出る
    kn = c.get("/api/knowwho?q=本人確認書類は何点必要？", headers=as_("u05")).json()["knowledge"]
    assert any(k["learned"].startswith("金額の上限を決めれば") for k in kn)



def test_request_attachments(c):
    import base64
    t = today(c)
    rid = c.post("/api/requests", json=request_body(t + timedelta(days=1)), headers=as_("u00")).json()["id"]
    pdf = b"%PDF-1.4 demo"
    up = c.post(f"/api/requests/{rid}/attachments", json={"filename": "企画書（案）.pdf", "data": base64.b64encode(pdf).decode()}, headers=as_("u00"))
    assert up.status_code == 201, up.text
    a = up.json()
    assert a["filename"] == "企画書（案）.pdf" and a["size"] == len(pdf) and a["content_type"] == "application/pdf"
    v = c.get(f"/api/requests/{rid}", headers=as_("u01")).json()  # 相手にも見える
    assert [x["id"] for x in v["attachments"]] == [a["id"]]
    d = c.get(f"/api/attachments/{a['id']}", headers=as_("u01"))
    assert d.status_code == 200 and d.content == pdf and "attachment" in d.headers["content-disposition"]
    assert c.get(f"/api/attachments/{a['id']}", headers=as_("u05")).status_code == 404  # 当事者だけ
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    assert c.post(f"/api/requests/{rid}/attachments", json={"filename": "run.exe", "data": b64(b"MZ")}, headers=as_("u00")).status_code == 422
    assert c.post(f"/api/requests/{rid}/attachments", json={"filename": "a.pdf", "data": "%%%"}, headers=as_("u00")).status_code == 422
    big = b64(b"0" * (10 * 1024 * 1024 + 1))
    assert c.post(f"/api/requests/{rid}/attachments", json={"filename": "big.pdf", "data": big}, headers=as_("u00")).status_code == 413
    for i in range(4):
        assert c.post(f"/api/requests/{rid}/attachments", json={"filename": f"m{i}.png", "data": b64(b"png")}, headers=as_("u01")).status_code == 201
    assert c.post(f"/api/requests/{rid}/attachments", json={"filename": "x.txt", "data": b64(b"x")}, headers=as_("u00")).status_code == 409  # 5つまで
    assert c.delete(f"/api/attachments/{a['id']}", headers=as_("u01")).status_code == 403  # 消せるのはつけた人
    assert c.delete(f"/api/attachments/{a['id']}", headers=as_("u00")).json()["ok"]
    assert len(c.get(f"/api/requests/{rid}", headers=as_("u00")).json()["attachments"]) == 4
    assert c.post("/api/admin/reset").status_code == 200  # デモを戻しても資料が残って詰まらない
