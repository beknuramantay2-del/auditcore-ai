import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from groq import Groq
from pydantic import BaseModel
from config import settings

groq_client = Groq(api_key=settings.GROQ_API_KEY)

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
    """

    completion = groq_client.beta.chat.completions.parse(
        model="llama-3.1-70b-versatile",
        messages=[
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": f"Interview history so far:\n{history_context}\n\nGenerate the next turn."}
        ],
        response_format=InterviewTurn,
    )
    return completion.choices[0].message.parsed

def generate_final_report_ru(fullname: str, role: str, transcript: str) -> str:
    # Используем ту же модель для моментальной сборки финального HR-отчета на русском
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
    
    response = groq_client.chat.completions.create(
        model="llama-3.1-70b-versatile",
        messages=[
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": f"Кандидат: {fullname}\nРоль: {role}\n\nЛог интервью:\n{transcript}"}
        ]
    )
    return response.choices[0].message.content

def send_email_report(candidate_name: str, role: str, report_text: str):
    try:
        msg = MIMEMultipart()
        msg['From'] = settings.SMTP_EMAIL
        msg['To'] = settings.SMTP_EMAIL  # Отправляем самому себе на monkifani@gmail.com
        msg['Subject'] = f"AuditCore AI Bot Report: {candidate_name} - {role}"
        
        msg.attach(MIMEText(report_text, 'plain', 'utf-8'))
        
        # Подключение к SMTP Gmail
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(settings.SMTP_EMAIL, settings.SMTP_PASSWORD)
        server.sendmail(settings.SMTP_EMAIL, settings.SMTP_EMAIL, msg.as_string())
        server.quit()
    except Exception as e:
        print(f"Ошибка отправки почты: {e}")
