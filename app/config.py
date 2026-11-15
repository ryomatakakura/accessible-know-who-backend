"""設定はすべて環境変数から読む（App Service の「アプリ設定」と Key Vault 参照で渡す）。"""
import os


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes")


class Settings:
    def __init__(self) -> None:
        # 本番：mysql+pymysql://user:pass@host:3306/seats?charset=utf8mb4（Key Vault 参照で渡す。パスワードの記号はURLエンコード）
        self.database_url = os.getenv("DATABASE_URL", "sqlite:///./seats.db")
        self.db_ssl = os.getenv("DB_SSL", "require").lower()   # require：TLSで相手を確かめる／disable：手元やCIのMySQL
        self.db_ssl_ca = os.getenv("DB_SSL_CA", "")             # ルート証明書のパスを変えたいときだけ指定
        # sso：App Service 認証（Entra ID）のヘッダーで本人を決める。demo：画面で人を切り替える
        self.auth_mode = os.getenv("AUTH_MODE", "demo").lower()
        self.demo_default_user = os.getenv("DEMO_DEFAULT_USER", "u00")
        self.tenant_name = os.getenv("TENANT_NAME", "うみかぜ銀行")
        self.seed_on_start = _bool("SEED_ON_START", True)
        self.allowed_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
        self.timezone = os.getenv("APP_TIMEZONE", "Asia/Tokyo")
        self.open_hour = int(os.getenv("OPEN_HOUR", "7"))     # 予約できる時間帯
        self.close_hour = int(os.getenv("CLOSE_HOUR", "21"))
        self.book_ahead_days = int(os.getenv("BOOK_AHEAD_DAYS", "14"))  # 何日先まで予約できるか

    @property
    def is_demo(self) -> bool:
        return self.auth_mode != "sso"


settings = Settings()
