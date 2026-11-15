"""本番データの取り込み（社員・部署・フロア）。何度実行しても同じ結果になる（上書き）。

使い方（DATABASE_URL を本番の接続先にしてから）：
  python tools/import_data.py people tools/people_sample.csv     # 社員と部署
  python tools/import_data.py floors tools/floors_sample.json    # フロア・座席・会議室
  python tools/import_data.py floors --sample                    # 見本のフロア（12F・13F）をそのまま使う

people の CSV（UTF-8、1行目は見出し）：email,name,department,title,admin,skills
  admin は 1 で分析画面を見られる人。メールは Entra ID のサインイン名（UPN）と同じにする。
  skills は「聞けること」を ; で区切る（例：非対面サービスの審査;コンプライアンス）。本番は Shpica の知見データから同期する想定。
floors の JSON：[{id, name, sort, layout, seats:[{id,name,area,kind,features,x,y}], rooms:[{id,name,capacity,x,y,w,h}]}]
  形は app/seed_data.py の FLOORS と同じ。ファイルから消えた座席は削除せず「使わない」にする（過去の予約を残すため）。
"""
import csv
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import Department, Expertise, Floor, Person, Room, Seat  # noqa: E402
from app.seed_data import COLORS, FLOORS  # noqa: E402


def _hid(prefix: str, s: str) -> str:
    return f"{prefix}{hashlib.sha1(s.encode()).hexdigest()[:8]}"


def import_people(path: str) -> None:
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    need = {"email", "name", "department"}
    if not rows or not need <= set(rows[0]):
        sys.exit(f"CSV の見出しに {', '.join(sorted(need))} が必要です")
    with SessionLocal() as db:
        depts = {d.name: d for d in db.scalars(select(Department))}
        people = {p.email.lower(): p for p in db.scalars(select(Person))}
        added = updated = 0
        for r in rows:
            email = r["email"].strip().lower()
            if not email or "@" not in email:
                print(f"  とばしました（メールが不正）：{r}")
                continue
            dname = r["department"].strip()
            if dname not in depts:
                depts[dname] = Department(id=_hid("d-", dname), name=dname)
                db.add(depts[dname])
                db.flush()
            p = people.get(email)
            if not p:
                p = Person(id=_hid("p-", email), email=email, color=COLORS[int(hashlib.sha1(email.encode()).hexdigest(), 16) % len(COLORS)])
                db.add(p)
                people[email] = p
                added += 1
            else:
                updated += 1
            p.name = r["name"].strip()
            p.dept_id = depts[dname].id
            p.title = (r.get("title") or "").strip()
            p.is_admin = (r.get("admin") or "").strip() in ("1", "true", "yes", "はい")
            db.flush()
            for topic in [t.strip() for t in (r.get("skills") or "").replace("；", ";").split(";") if t.strip()]:
                if not db.scalar(select(Expertise.id).where(Expertise.person_id == p.id, Expertise.topic == topic)):
                    db.add(Expertise(person_id=p.id, topic=topic[:60]))
        db.commit()
    print(f"社員：{added}人を追加、{updated}人を更新しました")


def import_floors(floors: list[dict]) -> None:
    with SessionLocal() as db:
        for f in floors:
            fl = db.get(Floor, f["id"]) or Floor(id=f["id"])
            fl.name, fl.sort, fl.layout = f["name"], f.get("sort", 0), f["layout"]
            db.add(fl)
            db.flush()
            keep = set()
            for s in f["seats"]:
                seat = db.get(Seat, s["id"]) or Seat(id=s["id"], floor_id=f["id"])
                seat.floor_id, seat.name, seat.area, seat.kind = f["id"], s["name"], s["area"], s["kind"]
                seat.features, seat.x, seat.y, seat.active = s.get("features", []), s["x"], s["y"], True
                db.add(seat)
                keep.add(s["id"])
            for old in db.scalars(select(Seat).where(Seat.floor_id == f["id"])):
                if old.id not in keep:
                    old.active = False
            for r in f["rooms"]:
                room = db.get(Room, r["id"]) or Room(id=r["id"], floor_id=f["id"])
                room.floor_id, room.name, room.capacity = f["id"], r["name"], r["capacity"]
                room.x, room.y, room.w, room.h = r["x"], r["y"], r["w"], r["h"]
                db.add(room)
            print(f"{f['id']}：座席 {len(f['seats'])}、会議室 {len(f['rooms'])}")
        db.commit()


def main(argv: list[str]) -> None:
    if len(argv) < 2 or argv[0] not in ("people", "floors"):
        sys.exit(__doc__)
    Base.metadata.create_all(engine)
    if argv[0] == "people":
        import_people(argv[1])
    elif argv[1] == "--sample":
        import_floors(FLOORS)
    else:
        with open(argv[1], encoding="utf-8") as f:
            import_floors(json.load(f))


if __name__ == "__main__":
    main(sys.argv[1:])
