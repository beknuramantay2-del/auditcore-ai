import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SMTP_EMAIL = os.getenv("SMTP_EMAIL")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
REPORT_EMAIL = "monkifani@gmail.com"
PORT = int(os.getenv("PORT", 10000))

MAX_TURNS = 4
CHEATER_SPEED_THRESHOLD = 12
