import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    TELEGRAM_TOKEN: str
    GROQ_API_KEY: str
    ADMIN_ID: int
    SMTP_EMAIL: str = "monkifani@gmail.com"
    SMTP_PASSWORD: str
    
    # Каприз Render: база должна лежать в постоянной директории, если она доступна
    @property
    def DATABASE_PATH(self) -> str:
        if os.path.exists("/data"):
            return "/data/auditcore.db"
        return "auditcore.db"

settings = Settings()
