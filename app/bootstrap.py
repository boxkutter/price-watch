from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Store, User
from app.security import hasher


def main() -> None:
    if (len(settings.app_secret) < 32 or settings.app_secret.startswith("replace-") or
        len(settings.admin_password) < 16 or settings.admin_password.startswith("replace-") or
        "change-me" in settings.database_url):
        raise SystemExit("Set unique APP_SECRET, ADMIN_PASSWORD, and database credentials")
    with SessionLocal.begin() as db:
        if not db.scalar(select(User).where(User.is_admin.is_(True))):
            db.add(User(email=settings.admin_email.lower(),
                        password_hash=hasher.hash(settings.admin_password), is_admin=True))
        if not db.scalar(select(Store).where(Store.retailer_id == "test.fixture")):
            db.add(Store(retailer_id="test.fixture", external_id="demo",
                         name="Synthetic demo store"))


if __name__ == "__main__":
    main()
