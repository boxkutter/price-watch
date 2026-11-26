from sqlalchemy import select

from app import declared_stores
from app.config import settings
from app.connectors import registry
from app.db import SessionLocal
from app.models import User
from app.security import hasher


def config_problems(config=None) -> list[str]:
    """What is wrong with the startup settings, as plain sentences. Never includes a value."""
    config = config or settings
    problems = []
    if config.app_secret.startswith("replace-"):
        problems.append("APP_SECRET is still the example placeholder. Set it to a random string "
                        "of at least 32 characters.")
    elif len(config.app_secret) < 32:
        problems.append("APP_SECRET is too short. It needs at least 32 random characters.")
    if config.admin_password.startswith("replace-"):
        problems.append("ADMIN_PASSWORD is still the example placeholder. Choose a password of "
                        "at least 16 characters; it is what you sign in to /admin with.")
    elif len(config.admin_password) < 16:
        problems.append("ADMIN_PASSWORD is too short. It needs at least 16 characters; it is "
                        "what you sign in to /admin with.")
    if "change-me" in config.database_url:
        problems.append("DATABASE_URL still contains the example password 'change-me'. Choose a "
                        "real database password and use the same one in POSTGRES_PASSWORD "
                        "(special characters in the URL must be percent-encoded).")
    return problems


def main() -> None:
    problems = config_problems()
    if problems:
        raise SystemExit("Cannot start: fix these settings in .env\n  - " + "\n  - ".join(problems))
    with SessionLocal.begin() as db:
        if not db.scalar(select(User).where(User.is_admin.is_(True))):
            db.add(User(email=settings.admin_email.lower(),
                        password_hash=hasher.hash(settings.admin_password), is_admin=True))
        registry.refresh(force=True)
        declared_stores.seed(db)  # the stores connectors declare in their manifests


if __name__ == "__main__":
    main()
