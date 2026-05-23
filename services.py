import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import httpx
from pydantic import BaseModel
from config import settings

# Описываем схему ответа
class InterviewTurn(BaseModel):
    question_text: str
    allowed_time_seconds: int
    is_interview_finished: bool

def get_ai_question(role: str, language: str, history_context: str) -> InterviewTurn:
    system_instruction = f"""
    You are an expert IT Technical Interviewer screening a candidate for a '{role}' position.
    Conduct the interview strictly in this language: '{language}'.
    
    CRITICAL RULES:
    1. If this is the first question, give them a realistic, production-level, practical coding or system logic task tailored to a Junior+/Middle {role}.
    2. If they provided a previous answer (check context), simulate code execution. Do NOT give a new task. Ask ONE highly specific follow-up question challenging their exact code implementation or logic to see if they genuinely understand it or just copy-pasted from ChatGPT.
    3. Keep tasks realistic (e.g., query optimization for Data Analyst, API routing/logic for Backend, async states for Frontend).
    4. Dynamically set 'allowed_time_seconds' between 45 and 120 seconds based on difficulty.
    5. After 3-4 total turns, set 'is_interview_finished' to true.
    
    You must output your response strictly as a JSON object matching this schema:
    {{
        "question_text": "string",
        "allowed_time_seconds": integer,
        "is_interview_finished": boolean
    }}
    """

    # Прямой HTTP-запрос к API Groq без использования их SDK
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "llama-3.1-70b-versatile",
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": f"Interview history so far:\n{history_context}\n\nGenerate the next turn."}
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.3
    }

    with httpx.Client(timeout=30.0) as client:
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        result = response.json()
        
        # Парсим JSON-строку из ответа модели в Pydantic объект
        content_string = result["choices"][0]["message"]["content"]
        return InterviewTurn.model_validate_json(content_string)

def generate_final_report_ru(fullname: str, role: str, transcript: str) -> str:
    system_instruction = """
    Вы — главный технический эксперт и HR-аналитик платформы AuditCore AI. 
    Вам предоставлен лог технического интервью кандидата.
    Напишите подробный отчет по кандидату СТРОГО НА РУССКОМ ЯЗЫКЕ.
    Включите:
    1. Технический балл (Technical Score) от 1 до 10.
    2. Индекс честности (Integrity Score) в % (оцените, писал ли он сам или копировал шаблоны ИИ).
    3. Список логических ошибок или аномалий.
    4. Итоговый вердикт (Рекомендован/Отклонен).
    """
    
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "llama-3.1-70b-versatile",
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": f"Кандидат: {fullname}\nРоль: {role}\n\nЛог интервью:\n{transcript}"}
        ],
        "temperature": 0.5
    }

    with httpx.Client(timeout=30.0) as client:
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        result = response.json()
        return result["choices"][0]["message"]["content"]

def send_email_report(candidate_name: str, role: str, report_text: str):
    try:
        msg = MIMEMultipart()
        msg['From'] = settings.SMTP_EMAIL
        msg['To'] = settings.SMTP_EMAIL
        msg['Subject'] = f"AuditCore AI Bot Report: {candidate_name} - {role}"
        
        msg.attach(MIMEText(report_text, 'plain', 'utf-8'))
        
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(settings.SMTP_EMAIL, settings.SMTP_PASSWORD)
        server.sendmail(settings.SMTP_EMAIL, settings.SMTP_EMAIL, msg.as_string())
        server.quit()
    except Exception as e:
        print(f"Ошибка отправки почты: {e}")
