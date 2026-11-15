from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

URL = settings.database_url
DIALECT = URL.split(":", 1)[0].split("+", 1)[0]  # sqlite / mysql / postgresql
# Azure Database for MySQL は TLS 必須。相手の証明書を、Azure が使うルート証明書
# （DigiCert Global Root CA／G2、Microsoft RSA Root 2017）で確かめる
AZURE_MYSQL_CA = Path(__file__).resolve().parent / "certs" / "azure-mysql-ca.pem"


def _connect_args() -> dict:
    if DIALECT == "sqlite":
        return {"check_same_thread": False}
    if DIALECT == "mysql" and settings.db_ssl != "disable":
        return {"ssl": {"ca": settings.db_ssl_ca or str(AZURE_MYSQL_CA)}}
    return {}


_pool = {} if DIALECT == "sqlite" else {"pool_size": 5, "max_overflow": 5, "pool_recycle": 1800}
# MySQL の既定（REPEATABLE READ）だと、席の行をロックした後の確認が古い状態を見ることがあるため READ COMMITTED にする
_iso = {"isolation_level": "READ COMMITTED"} if DIALECT == "mysql" else {}
engine = create_engine(URL, pool_pre_ping=True, connect_args=_connect_args(), **_pool, **_iso)

if DIALECT == "mysql":
    @event.listens_for(engine, "connect")
    def _utc(dbapi_conn, _record):
        # 時刻はすべてUTCで保存する（画面に出すときに日本時間へ直す）
        cur = dbapi_conn.cursor()
        cur.execute("SET time_zone = '+00:00'")
        cur.close()

SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def lock_row(db, stmt) -> None:
    """同時に同じ席・会議室を取りに来ても二重にならないよう、行をロックする（SQLite は書き込みが1本なので不要）。"""
    if db.bind.dialect.name in ("mysql", "postgresql"):
        db.execute(stmt.with_for_update())
