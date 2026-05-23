import asyncio
import json
import smtplib
import logging
import re

from openai import AsyncOpenAI
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from config import (
    OPENAI_API_KEY, SMTP_EMAIL, SMTP_PASSWORD,
    REPORT_EMAIL, CHEATER_SPEED_THRESHOLD, MAX_TURNS
)

logger = logging.getLogger(__name__)
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

ROLE_SKILLS = {
    "Backend Developer": (
        "Python (алгоритмы, asyncio, декораторы, генераторы), "
        "REST API design, SQL-оптимизация, ORM, кэширование, Docker"
    ),
    "Frontend Developer": (
        "JavaScript/TypeScript, React (хуки, жизненный цикл, Context/Redux), "
        "CSS архитектура, производительность браузера, Web APIs, bundlers"
    ),
    "Data Analyst": (
        "SQL (сложные JOIN, оконные функции, CTE), pandas/numpy, "
        "статистический анализ, визуализация данных, A/B тесты, ETL"
    ),
}

# ───────────────────────────────────────────────
# ГЕНЕРАЦИЯ ВОПРОСА ЧЕРЕЗ GPT-4o
# ───────────────────────────────────────────────

async def generate_question(
    role: str,
    language: str,
    turn: int,
    chat_history: list,
) -> dict:
    """
    chat_history — список сообщений в формате OpenAI:
    [{"role": "assistant", "content": "..."}, {"role": "user", "content": "..."}, ...]
    
    Возвращает:
    {
        "question_text": "...",
        "allowed_time_seconds": 90
    }
    """
    lang_rule = (
        "Веди интервью СТРОГО на русском языке."
        if language == "ru"
        else "Conduct the interview STRICTLY in English."
    )

    skills = ROLE_SKILLS.get(role, "general programming")

    system_prompt = f"""Ты — жёсткий технический интервьюер платформы AuditCore AI.
Роль кандидата: {role}
Навыки для проверки: {skills}
Всего вопросов в интервью: {MAX_TURNS}
Текущий вопрос: {turn} из {MAX_TURNS}

{lang_rule}

ПРАВИЛА:
- Каждый вопрос должен быть УНИКАЛЬНЫМ и СТРОГО основан на предыдущем ответе кандидата
- Никогда не повторяй предыдущие вопросы
- Задавай ТОЛЬКО практические задачи: найди баг в коде, оптимизируй функцию, напиши SQL-запрос
- С каждым туром сложность растёт
- Если кандидат ответил плохо — укажи на ошибку и углубись в ту же тему
- Если кандидат ответил хорошо — переходи к следующей теме из списка навыков
- Никогда не давай варианты ответа
- allowed_time_seconds выбирай динамически: простой вопрос = 45с, средний = 90с, сложный = 120с

Верни ТОЛЬКО валидный JSON без markdown:
{{
  "question_text": "текст практического вопроса",
  "allowed_time_seconds": 90
}}"""

    messages = [{"role": "system", "content": system_prompt}] + chat_history

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.7,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content.strip()
        data = json.loads(raw)
        return {
            "question_text": data.get("question_text", "Опишите свой подход к решению сложных задач."),
            "allowed_time_seconds": int(data.get("allowed_time_seconds", 90))
        }
    except Exception as e:
        logger.error(f"OpenAI question generation error: {e}")
        return {
            "question_text": "Напишите функцию на Python, которая находит все дубликаты в списке за O(n).",
            "allowed_time_seconds": 90
        }


# ───────────────────────────────────────────────
# ФИНАЛЬНЫЙ ОТЧЕТ ЧЕРЕЗ GPT-4o
# ───────────────────────────────────────────────

async def generate_final_report(candidate: dict) -> str:
    transcript = candidate.get("transcript", "")
    suspicious = candidate.get("suspicious_count", 0)
    timeouts = candidate.get("timeout_count", 0)
    role = candidate.get("role", "Unknown")
    name = candidate.get("name", "Unknown")

    prompt = f"""Ты — старший HR-аналитик системы AuditCore AI.
Перед тобой полная стенограмма технического интервью.

Имя кандидата: {name}
Роль: {role}
Флагов подозрительной скорости (< {CHEATER_SPEED_THRESHOLD} сек): {suspicious}
Таймаутов (не ответил вовремя): {timeouts}

СТЕНОГРАММА:
{transcript}

Напиши ПОЛНЫЙ ОТЧЁТ СТРОГО НА РУССКОМ ЯЗЫКЕ:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 ОТЧЁТ AUDITCORE AI
Кандидат: {name}
Роль: {role}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1️⃣ ОЦЕНКА ТЕХНИЧЕСКИХ НАВЫКОВ: [X/10]
[Детальное обоснование — что знает, чего не знает]

2️⃣ ИНДЕКС ЧЕСТНОСТИ: [X%]
Флагов скорости: {suspicious} | Таймаутов: {timeouts}
[Анализ поведенческих паттернов — были ли признаки использования ИИ]

3️⃣ РАЗБОР ОТВЕТОВ ПО КАЖДОМУ ВОПРОСУ:
[Для каждого вопроса: что спросили → что ответил → оценка ответа]

4️⃣ ФИНАЛЬНЫЙ ВЕРДИКТ: [ПРОФЕССИОНАЛ / ПОДОЗРИТЕЛЬНЫЙ / ЧИТЕР / СЛАБЫЙ КАНДИДАТ]

5️⃣ РЕКОМЕНДАЦИЯ HR:
[Конкретное действие: нанять / отказать / очное интервью / испытательный срок]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"OpenAI report generation error: {e}")
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

    msg.attach(MIMEText(report_text, "plain", "utf-8"))

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
        

    
