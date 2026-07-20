"""Application configuration, loaded from environment variables (.env)."""
import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    # Database — defaults point at the Docker Postgres exposed on host port 5433
    # so it does not clash with a locally-installed Postgres on 5432.
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg2://erp:erp@localhost:5433/hardware_erp",
    )
    secret_key: str = os.getenv("SECRET_KEY", "dev-secret-change-me")
    app_name: str = os.getenv("APP_NAME", "Hardware ERP")

    # Seeded admin account (created on first startup if it does not exist)
    admin_username: str = os.getenv("ADMIN_USERNAME", "admin")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "admin123")


settings = Settings()
