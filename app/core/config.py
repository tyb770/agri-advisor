# app/core/config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str
    DEBUG: bool = False
    GOOGLE_API_KEY: str
    WHATSAPP_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    WHATSAPP_VERIFY_TOKEN: str = ""
    # Redis — used for Celery broker AND onboarding sessions
    # Default matches docker-compose; override in .env for production
    REDIS_URL: str = "redis://localhost:6379/0"

    class Config:
        env_file = ".env"
        extra = "ignore"  # optional safety

settings = Settings()