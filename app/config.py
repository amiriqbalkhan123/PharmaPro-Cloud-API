from pydantic_settings import BaseSettings
from typing import List
import os


class Settings(BaseSettings):
    DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://pharmapro:password@localhost:5432/pharmapro_cloud")
    SECRET_KEY: str = os.getenv("SECRET_KEY", "your-super-secret-key-change-this")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440  # 24 hours for mobile

    APP_NAME: str = "PharmaPro Cloud API"
    DEBUG: bool = os.getenv("DEBUG", "False").lower() == "true"
    # CORS
    ALLOWED_ORIGINS: List[str] = [
        "http://localhost:3000",
        "http://localhost:5000",
        "http://127.0.0.1:5000",
        "*"  # For testing only - restrict in production
    ]

    class Config:
        env_file = ".env"


settings = Settings()