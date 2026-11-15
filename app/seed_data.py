"""デモ用の合成データ（架空の「うみかぜ銀行」）。実在の企業・人物とは関係ありません。

静的データ（部署・社員・フロア・座席・会議室）と、日付に合わせて作る動的データ（在席・予約・会議・過去30日の履歴）。
"""
import random
from datetime import date, datetime, time, timedelta, timezone

DOMAIN = "umikaze-bank.example"
DEPARTMENTS = [("sales", "営業部"), ("mkt", "マーケティング部"), ("dev", "開発部"), ("hr", "人事部"), ("plan", "経営企画部"), ("ga", "総務部")]
_NAMES = """高木 拓海,plan,マネージャー;近藤 恵子,sales,リーダー;加藤 陽子,sales,メンバー;田中 太郎,sales,メンバー;渡辺 由美,sales,メンバー;清水 陸,sales,メンバー;山下 美月,sales,マネージャー;石川 蓮,sales,メンバー;福田 春香,sales,メンバー
遠藤 浩一,mkt,マネージャー;石井 翔太,mkt,メンバー;坂本 裕子,mkt,メンバー;太田 健吾,mkt,リーダー;青木 咲,mkt,メンバー;林 結衣,mkt,メンバー;上田 菜々,mkt,メンバー
中島 亮,dev,マネージャー;前田 紗希,dev,リーダー;藤田 誠,dev,メンバー;岡本 彩,dev,メンバー;松田 大輝,dev,メンバー;村上 葵,dev,メンバー;長谷川 悠,dev,メンバー;斎藤 光,dev,メンバー;井上 芽依,dev,メンバー;木村 颯,dev,メンバー;三浦 湊,dev,メンバー;原田 誠也,dev,リーダー;野口 千尋,dev,メンバー;菅原 樹,dev,メンバー
小野 真理,hr,マネージャー;山崎 拓,hr,メンバー;森 美穂,hr,メンバー;佐野 由佳,hr,メンバー
池田 隆,plan,メンバー;橋本 美咲,plan,リーダー;阿部 翼,plan,メンバー;西村 健,plan,メンバー;河野 沙織,plan,メンバー
大塚 修,ga,マネージャー;内田 絵美,ga,メンバー;杉本 大地,ga,メンバー;平野 琴音,ga,メンバー;久保 智也,ga,メンバー;松井 杏,ga,メンバー;竹内 亮介,ga,メンバー;千葉 莉子,ga,メンバー;岩崎 航,ga,メンバー"""
COLORS = ["#f2994a", "#27ae60", "#2d9cdb", "#a061d6", "#e5605a", "#1f9d74", "#7b5fe0", "#d4a017", "#3fb6dc", "#d6649a", "#5b6fd6", "#0f9d8a"]


def people():
    rows = [r.split(",") for line in _NAMES.splitlines() for r in line.split(";")]
    out = []
    for i, (name, dept, title) in enumerate(rows):
        out.append({"id": f"u{i:02d}", "name": name, "email": f"user{i:02d}@{DOMAIN}", "dept": dept, "title": title,
                    "color": COLORS[i % len(COLORS)], "is_admin": i == 0})
    return out


# ---------- フロア ----------
def _floor(fid, name, sort, rooms, islands, booths, lounge, counter, core, areas, plants):
    seats, n = [], 0
    furn = []
    for (x0, y0, cols) in islands:
        furn.append({"k": "island", "x": x0, "y": y0, "w": cols * 64, "h": 56})
        for c in range(cols):
            for side, y in (("n", y0 - 18), ("s", y0 + 74)):
                n += 1
                feats = ["モニター"] if c % 2 == 0 else []
                seats.append({"id": f"{fid}-D{n:02d}", "name": f"Innovation Labo-{n:02d}", "area": "Innovation Labo", "kind": "desk",
                              "features": feats, "x": x0 + 32 + c * 64, "y": y})
    for i, (x, y) in enumerate(booths):
        furn.append({"k": "booth", "x": x, "y": y, "w": 90, "h": 70})
        seats.append({"id": f"{fid}-B{i + 1:02d}", "name": f"Focus Booth-{i + 1:02d}", "area": "Focus Booth", "kind": "booth",
                      "features": ["個室", "モニター"], "x": x + 30, "y": y + 35})
    for i, (cx, cy) in enumerate(lounge):
        furn.append({"k": "table", "x": cx, "y": cy})
        for j, dx in enumerate((-42, 42)):
            seats.append({"id": f"{fid}-L{i * 2 + j + 1:02d}", "name": f"Lounge-{i * 2 + j + 1:02d}", "area": "Lounge", "kind": "lounge",
                          "features": ["ソファ"], "x": cx + dx, "y": cy})
    if counter:
        x0, y0, count = counter
        furn.append({"k": "counter", "x": x0, "y": y0, "w": count * 76, "h": 24})
        for i in range(count):
            seats.append({"id": f"{fid}-W{i + 1:02d}", "name": f"Window Counter-{i + 1:02d}", "area": "Window Counter", "kind": "desk",
                          "features": ["窓側"], "x": x0 + 38 + i * 76, "y": y0 - 18})
    room_rows = [{"id": f"{fid}-R-{r[0]}", "name": r[0], "x": r[1], "y": r[2], "w": r[3], "h": r[4], "capacity": r[5]} for r in rooms]
    layout = {"w": 1200, "h": 760, "slab": [20, 20, 1160, 720], "core": core, "areas": areas, "furniture": furn, "plants": plants}
    return {"id": fid, "name": name, "sort": sort, "layout": layout, "seats": seats, "rooms": room_rows}


FLOORS = [
    _floor("12F", "本社 12F", 1,
           rooms=[("Mozart", 40, 40, 170, 120, 6), ("Bach", 220, 40, 140, 120, 4), ("Chopin", 370, 40, 190, 120, 8), ("Vivaldi", 570, 40, 240, 120, 12),
                  ("Haydn", 40, 500, 170, 130, 4), ("Verdi", 220, 500, 180, 130, 6)],
           islands=[(460, 228, 4), (760, 228, 4), (460, 348, 4), (760, 348, 4), (460, 468, 4), (760, 468, 4)],
           booths=[(1070, 64 + i * 80) for i in range(6)],
           lounge=[(140, 290), (300, 290), (140, 400), (300, 400)],
           counter=(430, 668, 8),
           core=[830, 40, 210, 120],
           areas=[{"name": "Innovation Labo", "x": 430, "y": 186, "w": 610, "h": 400, "tone": "work"},
                  {"name": "Lounge", "x": 40, "y": 186, "w": 360, "h": 290, "tone": "lounge"},
                  {"name": "Window Counter", "x": 430, "y": 610, "w": 610, "h": 110, "tone": "window"},
                  {"name": "Focus Booth", "x": 1060, "y": 48, "w": 110, "h": 496, "tone": "booth"}],
           plants=[[415, 196], [1050, 580], [30, 660], [415, 700], [1160, 700]]),
    _floor("13F", "本社 13F", 2,
           rooms=[("Brahms", 40, 40, 260, 120, 10), ("Handel", 310, 40, 200, 120, 6), ("Ravel", 520, 40, 160, 120, 4), ("Liszt", 40, 500, 360, 130, 16)],
           islands=[(460, 228, 4), (760, 228, 4), (460, 368, 4), (760, 368, 4)],
           booths=[(1070, 64 + i * 80) for i in range(4)],
           lounge=[(140, 300), (300, 300), (220, 410)],
           counter=(430, 668, 6),
           core=[830, 40, 210, 120],
           areas=[{"name": "Innovation Labo", "x": 430, "y": 186, "w": 610, "h": 300, "tone": "work"},
                  {"name": "Lounge", "x": 40, "y": 186, "w": 360, "h": 290, "tone": "lounge"},
                  {"name": "Window Counter", "x": 430, "y": 610, "w": 610, "h": 110, "tone": "window"},
                  {"name": "Focus Booth", "x": 1060, "y": 48, "w": 110, "h": 336, "tone": "booth"}],
           plants=[[415, 196], [1050, 500], [30, 660], [1160, 700]]),
]

STATUSES = {"office": "オフィス", "remote": "リモート", "trip": "出張", "out": "外出", "leave": "休暇"}

# ---------- 聞けること（本番は Shpica の知見データから同期する。ここでは架空） ----------
# 分野名 → 「今日やること」の文から分野を見つけるための言葉
TOPICS = {
    "非対面サービスの審査": ["非対面", "オンライン", "本人確認", "ekyc", "新サービス", "アプリ申込"],
    "コンプライアンス": ["コンプラ", "法令", "規制", "金融庁", "リスク管理"],
    "法人営業": ["法人", "営業", "提案", "商談", "取引先"],
    "住宅ローン": ["住宅", "ローン", "融資"],
    "事業承継": ["承継", "後継", "m&a"],
    "補助金申請": ["補助金", "助成", "申請"],
    "データ分析": ["データ", "分析", "集計", "ダッシュボード", "bi"],
    "生成AI活用": ["生成ai", "chatgpt", "copilot", "プロンプト", "ai活用"],
    "アプリ開発": ["アプリ", "開発", "システム", "api"],
    "セキュリティ審査": ["セキュリティ", "情報漏洩", "脆弱", "認証"],
    "採用": ["採用", "面接", "中途", "新卒"],
    "研修設計": ["研修", "育成", "教育", "オンボーディング"],
    "人事制度": ["評価", "人事", "制度", "等級"],
    "中期経営計画": ["中計", "中期", "経営計画", "戦略"],
    "予算策定": ["予算", "コスト", "費用"],
    "広告・販促": ["広告", "販促", "キャンペーン", "チラシ"],
    "SNS運用": ["sns", "instagram", "発信", "動画"],
    "顧客アンケート分析": ["アンケート", "顧客の声", "nps", "満足度"],
    "規程改定": ["規程", "規則", "ルール", "社内規定"],
    "契約書チェック": ["契約", "法務", "nda"],
    "オフィス・ファシリティ": ["オフィス", "座席", "レイアウト", "移転", "ファシリティ"],
}
_DEPT_TOPICS = {
    "sales": ["法人営業", "住宅ローン", "事業承継", "コンプライアンス"],
    "mkt": ["広告・販促", "SNS運用", "顧客アンケート分析", "データ分析"],
    "dev": ["アプリ開発", "セキュリティ審査", "データ分析", "生成AI活用"],
    "hr": ["採用", "研修設計", "人事制度"],
    "plan": ["中期経営計画", "予算策定", "データ分析", "生成AI活用"],
    "ga": ["規程改定", "契約書チェック", "オフィス・ファシリティ", "補助金申請"],
}
# デモの筋：経営企画の高木さんが「非対面サービスの審査」を聞きたい → 12Fに2人、木曜に来る人が1人
_DEMO_TOPICS = {"u01": ["非対面サービスの審査", "コンプライアンス"], "u18": ["非対面サービスの審査", "セキュリティ審査"],
                "u10": ["非対面サービスの審査"], "u40": ["補助金申請"], "u35": ["生成AI活用"], "u17": ["研修設計"]}


def match_topics(text: str) -> list[str]:
    """「今日やること」の文から、当てはまる分野を最大3つ選ぶ。"""
    t = (text or "").replace(" ", "").replace("　", "").lower()
    if not t:
        return []
    scores = {}
    for topic, words in TOPICS.items():
        sc = 5 if (topic.lower() in t or (len(t) >= 2 and t in topic.lower())) else 0
        sc += sum(1 for w in words if w in t)
        if sc:
            scores[topic] = sc
    if not scores:
        return []
    best = max(scores.values())
    return [k for k, v in sorted(scores.items(), key=lambda kv: -kv[1]) if v >= max(1, best - 1)][:3]


def expertise():
    out = []
    for i, p in enumerate(people()):
        r = random.Random(1000 + i)
        own = r.sample(_DEPT_TOPICS[p["dept"]], 2)
        other = r.choice([t for t in TOPICS if t not in _DEPT_TOPICS[p["dept"]]])
        topics = list(dict.fromkeys(_DEMO_TOPICS.get(p["id"], []) + own + ([other] if r.random() < 0.6 else [])))[:4]
        for t in topics:
            out.append({"person_id": p["id"], "topic": t, "docs": r.randint(1, 9), "answers": r.randint(0, 14)})
    return out


# ---------- 日付に合わせて作るデータ ----------
def _jst(day: date, h: int, m: int, tz) -> datetime:
    return datetime.combine(day, time(h, m), tzinfo=tz).astimezone(timezone.utc)


def dynamic(today: date, tz):
    """今日の在席・予約・会議と、過去30日の履歴を作る（同じ日付なら毎回同じ結果）。"""
    rnd = random.Random(today.toordinal())
    ps = people()
    ids = [p["id"] for p in ps]
    seats = [s for f in FLOORS for s in f["seats"]]
    by_floor = {f["id"]: [s["id"] for s in f["seats"]] for f in FLOORS}
    rooms = [r for f in FLOORS for r in f["rooms"]]
    presence, reservations, bookings = [], [], []

    # 過去30日（平日）の履歴：曜日で出社率が変わる
    factor = {0: 0.55, 1: 0.68, 2: 0.74, 3: 0.66, 4: 0.42}
    for back in range(30, 0, -1):
        d = today - timedelta(days=back)
        if d.weekday() > 4:
            continue
        hr = random.Random(d.toordinal())
        crowd = [i for i in ids if i != "u00" and hr.random() < factor[d.weekday()]]
        free = list(seats)
        hr.shuffle(free)
        free.sort(key=lambda s: {"desk": 0, "booth": 0.5, "lounge": 1.1}[s["kind"]] + hr.random() * 2.2)
        for pid, s in zip(crowd, free):
            presence.append({"person_id": pid, "day": d, "status": "office", "seat_id": s["id"],
                             "checked_in_at": _jst(d, 8 + hr.randint(0, 2), hr.choice([0, 15, 30, 45]), tz)})
            if hr.random() < 0.35:
                reservations.append({"seat_id": s["id"], "person_id": pid, "day": d, "start": "09:00", "end": "18:00", "all_day": True})
        others = [i for i in ids if i not in crowd]
        for pid in others:
            st = hr.choices(["remote", "out", "trip", "leave", None], [6, 1, 0.6, 0.4, 2])[0]
            if st:
                presence.append({"person_id": pid, "day": d, "status": st, "seat_id": None, "checked_in_at": None})
        # 予約したのに来なかった人（ノーショー）
        for _ in range(hr.randint(1, 3)):
            pid = hr.choice(others)
            s = hr.choice(free[len(crowd):] or free)
            reservations.append({"seat_id": s["id"], "person_id": pid, "day": d, "start": "09:00", "end": "18:00", "all_day": True})

    # 今日
    office12 = ["u01", "u02", "u03", "u09", "u12", "u13", "u16", "u17", "u18", "u19", "u20", "u31", "u34", "u35", "u38", "u39"]
    office13 = ["u21", "u22", "u23", "u24", "u40", "u41"]
    seats12 = [s for s in by_floor["12F"] if "-D" in s]
    seats13 = [s for s in by_floor["13F"] if "-D" in s]
    pick12 = rnd.sample(seats12, len(office12)) + rnd.sample([s for s in by_floor["12F"] if "-B" in s or "-L" in s], 3)
    pick13 = rnd.sample(seats13, len(office13))
    for pid, sid in list(zip(office12, pick12)) + list(zip(office13, pick13)) + list(zip(["u42", "u43", "u44"], pick12[len(office12):])):
        presence.append({"person_id": pid, "day": today, "status": "office", "seat_id": sid,
                         "checked_in_at": _jst(today, 8 + rnd.randint(0, 1), rnd.choice([5, 20, 35, 50]), tz)})
    for pid, st in [("u10", "remote"), ("u11", "remote"), ("u14", "remote"), ("u25", "remote"), ("u26", "remote"), ("u32", "remote"), ("u36", "remote"),
                    ("u04", "out"), ("u05", "out"), ("u15", "out"), ("u06", "trip"), ("u07", "trip"), ("u33", "leave")]:
        presence.append({"person_id": pid, "day": today, "status": st, "seat_id": None, "checked_in_at": None})

    used = {p["seat_id"] for p in presence if p["day"] == today and p["seat_id"]}
    free12 = [s for s in seats12 if s not in used]
    # 今日：予約済みでまだ来ていない人
    for pid, sid in zip(["u27", "u28", "u37"], rnd.sample(free12, 3)):
        reservations.append({"seat_id": sid, "person_id": pid, "day": today, "start": "13:00", "end": "18:00", "all_day": False})
    # この先の予約
    for off in range(1, 8):
        d = today + timedelta(days=off)
        if d.weekday() > 4:
            continue
        for pid, sid in zip(rnd.sample(ids[1:], 9), rnd.sample(seats12 + seats13, 9)):
            reservations.append({"seat_id": sid, "person_id": pid, "day": d, "start": "09:00", "end": "18:00", "all_day": True})
    # 自分（u00）の予約を1件
    nxt = next(today + timedelta(days=k) for k in range(2, 9) if (today + timedelta(days=k)).weekday() < 5)
    reservations = [r for r in reservations if not (r["day"] == nxt and r["seat_id"] == "12F-B02")]
    reservations.append({"seat_id": "12F-B02", "person_id": "u00", "day": nxt, "start": "09:00", "end": "18:00", "all_day": True})

    # デモの筋：石井さん（非対面サービスの審査に詳しい・今日はリモート）が次の木曜に12Fへ来る
    thu = next(today + timedelta(days=k) for k in range(1, 8) if (today + timedelta(days=k)).weekday() == 3)
    reservations = [r for r in reservations if not (r["day"] == thu and (r["seat_id"] == "12F-D20" or r["person_id"] == "u10"))]
    reservations.append({"seat_id": "12F-D20", "person_id": "u10", "day": thu, "start": "09:00", "end": "18:00", "all_day": True})

    # 過去30日の相談の記録（使われ始めて、座席から始まる相談が増えていく流れ）
    consultations = []
    skills = {}
    for e in expertise():
        skills.setdefault(e["person_id"], []).append(e["topic"])
    dept = {p["id"]: p["dept"] for p in ps}
    experts = sorted(skills)
    for back in range(30, 0, -1):
        d = today - timedelta(days=back)
        if d.weekday() > 4:
            continue
        cr = random.Random(d.toordinal() * 3 + 1)
        week = (30 - back) // 7  # 0〜4：だんだん使われる
        for _ in range(2 + week + cr.randint(0, 2)):
            ex = cr.choice(experts)
            others = [i for i in ids if i != ex]
            cross = [i for i in others if dept[i] != dept[ex]]
            same = [i for i in others if dept[i] == dept[ex]] or others
            asker = cr.choice(cross if cr.random() < 0.3 + 0.08 * week else same)  # 部署をまたぐ相談が少しずつ増える
            consultations.append({"asker_id": asker, "expert_id": ex, "topic": cr.choice(skills[ex]), "resolved": cr.random() < 0.8,
                                  "source": "seat" if cr.random() < 0.3 + 0.1 * week else "profile", "day": d,
                                  "created_at": _jst(d, cr.randint(10, 17), cr.choice([0, 20, 40]), tz)})

    # デモの筋：近藤さん・藤田さんは「非対面サービスの審査」でよく頼られている
    past = [d for d in (today - timedelta(days=k) for k in range(1, 29)) if d.weekday() < 5]
    for ex, n, askers in (("u01", 6, ["u20", "u35", "u24", "u12", "u41", "u30"]), ("u18", 3, ["u02", "u37", "u13"])):
        for j in range(n):
            d = past[(j * 4 + (1 if ex == "u18" else 0)) % len(past)]
            consultations.append({"asker_id": askers[j], "expert_id": ex, "topic": "非対面サービスの審査", "resolved": True,
                                  "source": "seat" if j % 2 == 0 else "profile", "day": d, "created_at": _jst(d, 11 + j % 5, 10, tz)})

    # 会議室（今日と明日）
    titles = ["週次定例", "1on1", "採用面接", "部長会", "顧客打合せ", "企画レビュー", "全社研修", "プロジェクト定例"]
    for d in (today, today + timedelta(days=1)):
        br = random.Random(d.toordinal() + 7)
        for r in rooms:
            h = 9
            while h < 18:
                if br.random() < 0.45:
                    dur = br.choice([1, 1, 2, 3])
                    end = min(h + dur, 19)
                    bookings.append({"room_id": r["id"], "organizer_id": br.choice(ids), "day": d, "start": f"{h:02d}:00", "end": f"{end:02d}:00", "title": br.choice(titles)})
                    h = end
                else:
                    h += 1
    return presence, reservations, bookings, consultations
