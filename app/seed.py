"""Idempotent seed: create the initial admin user if it does not exist."""
from .auth import hash_password
from .config import settings
from .database import SessionLocal
from .models import User


def main() -> None:
    db = SessionLocal()
    try:
        existing = db.query(User).filter_by(username=settings.admin_username).first()
        if existing:
            print(f"[seed] admin user '{settings.admin_username}' already exists")
            return
        user = User(
            username=settings.admin_username,
            full_name="Administrator",
            password_hash=hash_password(settings.admin_password),
            role="admin",
            is_active=True,
        )
        db.add(user)
        db.commit()
        print(f"[seed] created admin user '{settings.admin_username}'")
    finally:
        db.close()


if __name__ == "__main__":
    main()
