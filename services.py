import asyncio
import json
import smtplib
import logging
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import google.generativeai as genai

from config import (
    GEMINI_API_KEY, SMTP_EMAIL, SMTP_PASSWORD,
    REPORT_EMAIL, CHEATER_SPEED_THRESHOLD
)

logger = logging.getLogger(__name__)
genai.configure(api_key=GEMINI_API_KEY)

flash_model = genai.GenerativeModel('gemini-2.0-flash')
pro_model = genai.GenerativeModel('gemini-2.0-flash')

# ───────────────────────────────────────────────
# ГЕНЕРАЦИЯ ВОПРОСА ЧЕРЕЗ GEMINI FLASH
# ───────────────────────────────────────────────

async def generate_question(
    role: str,
    language: str,
    turn: int,
    previous_answer: str = "",
    timeout: bool = False,
    transcript: str = ""
) -> dict:
    """
    Возвращает словарь:
    {
        "question_text": "...",
        "allowed_time_seconds": 90
    }
    """
    lang_instruction = "Respond STRICTLY in Russian." if language == "ru" else "Respond STRICTLY in English."

    role_context = {
        "Backend Developer": "Python algorithms, API optimization, database queries (SQL), async programming, OOP design",
        "Frontend Developer": "JavaScript/TypeScript, React architecture, UI/UX logic, CSS optimization, browser APIs",
        "Data Analyst": "SQL queries, data cleaning logic, pandas/numpy, statistical reasoning, visualization logic"
    }.get(role, "general programming")

    if timeout:
        context = f"The candidate FAILED to answer in time (timeout). Their previous answer was empty. Give a slightly easier but still practical question."
    elif previous_answer:
        context = f"Candidate's previous answer: '''{previous_answer}'''. Analyze it, point out any errors briefly, then ask a sharp follow-up question that tests deeper understanding."
    else:
        context = "This is the FIRST question. Start with a practical, realistic coding task."

    prompt = f"""
You are a strict technical interviewer at AuditCore AI.
Role being assessed: {role}
Skills to cover: {role_context}
Turn number: {turn} of 4
{lang_instruction}
{context}

Return ONLY a valid JSON object (no markdown, no code blocks) in this exact format:
{{
  "question_text": "your practical question here",
  "allowed_time_seconds": 90
}}

Rules:
- allowed_time_seconds must be an integer between 45 and 120
- Make the question progressively harder each turn
- No multiple choice. Demand code or detailed explanation.
- Keep question_text concise but technically deep.
"""

    try:
        response = await flash_model.generate_content_async(prompt)
        raw = response.text.strip()
        # Чистим markdown если модель всё равно добавила
        raw = re.sub(r"```json|```", "", raw).strip()
        data = json.loads(raw)
        return {
            "question_text": data.get("question_text", "Explain your approach to this role."),
            "allowed_time_seconds": int(data.get("allowed_time_seconds", 90))
        }
    except Exception as e:
        logger.error(f"Gemini Flash error: {e}")
        return {
            "question_text": "Describe your most complex technical project and the problems you solved.",
            "allowed_time_seconds": 90
        }


# ───────────────────────────────────────────────
# ФИНАЛЬНЫЙ ОТЧЕТ ЧЕРЕЗ GEMINI PRO
# ───────────────────────────────────────────────

async def generate_final_report(candidate: dict) -> str:
    transcript = candidate.get("transcript", "")
    suspicious = candidate.get("suspicious_count", 0)
    timeouts = candidate.get("timeout_count", 0)
    role = candidate.get("role", "Unknown")
    name = candidate.get("name", "Unknown")

    prompt = f"""
Ты — старший HR-аналитик системы AuditCore AI.
Перед тобой полная стенограмма технического интервью кандидата.

Имя: {name}
Роль: {role}
Количество флагов подозрительной скорости ответа (< {CHEATER_SPEED_THRESHOLD} сек): {suspicious}
Количество таймаутов (не ответил вовремя): {timeouts}

Стенограмма:
{transcript}

НАПИШИ ПОЛНЫЙ ОТЧЁТ СТРОГО НА РУССКОМ ЯЗЫКЕ в следующем формате:

---
ОТЧЁТ AUDITCORE AI
Кандидат: {name}
Роль: {role}

1. ОЦЕНКА ТЕХНИЧЕСКИХ НАВЫКОВ (1-10): [число]
   Обоснование: [3-4 предложения]

2. ИНДЕКС ЧЕСТНОСТИ (0-100%): [число]%
   Метрики: Флагов скорости: {suspicious} | Таймаутов: {timeouts}
   Обоснование: [2-3 предложения]

3. АНАЛИЗ КОДА И ОТВЕТОВ:
   [Детальный разбор каждого ответа кандидата: правильность, глубина понимания, качество кода]

4. ФИНАЛЬНЫЙ ВЕРДИКТ: [ПРОФЕССИОНАЛ / ПОДОЗРИТЕЛЬНЫЙ / ЧИТЕР / СЛАБЫЙ КАНДИДАТ]
   Рекомендация HR: [конкретная рекомендация: нанять / отказать / провести очное интервью]
---
"""

    try:
        response = await pro_model.generate_content_async(prompt)
        return response.text
    except Exception as e:
        logger.error(f"Gemini Pro error: {e}")
        return f"Ошибка генерации отчёта: {e}"


# ───────────────────────────────────────────────
# ОТПРАВКА EMAIL
# ───────────────────────────────────────────────

async def send_email_report(candidate: dict, report_text: str):
    if not SMTP_EMAIL or not SMTP_PASSWORD:
        logger.warning("SMTP не настроен. Пропускаем отправку email.")
        return

    name = candidate.get("name", "Unknown")
    role = candidate.get("role", "Unknown")
    subject = f"AuditCore AI Bot Report: {name} - {role}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_EMAIL
    msg["To"] = REPORT_EMAIL

    body = MIMEText(report_text, "plain", "utf-8")
    msg.attach(body)

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _send_smtp, msg)
        logger.info(f"Email отправлен для {name}")
    except Exception as e:
        logger.error(f"Ошибка отправки email: {e}")

def _send_smtp(msg):
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SMTP_EMAIL, SMTP_PASSWORD)
        server.send_message(msg)
