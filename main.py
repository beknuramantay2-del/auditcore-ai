import asyncio
import time
import sqlite3
import logging
import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.enums import ParseMode
import google.generativeai as genai

# Загружаем секретные ключи
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

MAX_QUESTIONS = 3
TIME_LIMIT = 90 # Секунд на ответ
CHEATER_TIME = 15 # Если ответил быстрее, значит скопировал

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
router = Router()

genai.configure(api_key=GEMINI_API_KEY)
flash_model = genai.GenerativeModel('gemini-1.5-flash')
pro_model = genai.GenerativeModel('gemini-1.5-pro')

def init_db():
    conn = sqlite3.connect('auditcore.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER,
            name TEXT,
            phone TEXT,
            github TEXT,
            exp TEXT,
            suspicious_count INTEGER DEFAULT 0,
            transcript TEXT DEFAULT '',
            status TEXT DEFAULT 'В процессе'
        )
    ''')
    conn.commit()
    conn.close()

class InterviewFlow(StatesGroup):
    waiting_name = State()
    waiting_phone = State()
    waiting_github = State()
    waiting_exp = State()
    ready_to_start = State()
    interviewing = State()

chat_sessions = {}
interview_data = {}

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await message.answer(
        "👋 <b>Добро пожаловать в AuditCore AI!</b>\n\n"
        "Мы создаем систему честного найма. Сейчас вы пройдете SkillPass для роли <b>Backend Python Developer</b>.\n\n"
        "Для начала введите ваше <b>ФИО</b>:",
        parse_mode=ParseMode.HTML
    )
    await state.set_state(InterviewFlow.waiting_name)

@router.message(InterviewFlow.waiting_name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("📞 Укажите ваш контактный телефон:")
    await state.set_state(InterviewFlow.waiting_phone)

@router.message(InterviewFlow.waiting_phone)
async def process_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text)
    await message.answer("💻 Пришлите ссылку на ваш GitHub:")
    await state.set_state(InterviewFlow.waiting_github)

@router.message(InterviewFlow.waiting_github)
async def process_github(message: Message, state: FSMContext):
    await state.update_data(github=message.text)
    await message.answer("⏱ Укажите ваш опыт работы с Python (в годах, например: 2):")
    await state.set_state(InterviewFlow.waiting_exp)

@router.message(InterviewFlow.waiting_exp)
async def process_exp(message: Message, state: FSMContext):
    data = await state.update_data(exp=message.text)
    
    conn = sqlite3.connect('auditcore.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO candidates (tg_id, name, phone, github, exp)
        VALUES (?, ?, ?, ?, ?)
    ''', (message.from_user.id, data['name'], data['phone'], data['github'], data['exp']))
    conn.commit()
    conn.close()

    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Начать SkillPass")]],
        resize_keyboard=True
    )
    
    await message.answer(
        "⚠️ <b>ВНИМАНИЕ. ПРАВИЛА SKILLPASS:</b>\n\n"
        "1. На ответ на каждый вопрос у вас есть строго <b>90 секунд</b>.\n"
        "2. Система фиксирует скорость ответа. Слишком быстрый ответ на сложный код будет расценен как читерство.\n"
        "3. Использование ChatGPT или других ИИ-помощников приведет к автоматической дисквалификации.\n\n"
        "Готовы? Жмите кнопку ниже.",
        reply_markup=kb,
        parse_mode=ParseMode.HTML
    )
    await state.set_state(InterviewFlow.ready_to_start)

@router.message(InterviewFlow.ready_to_start, F.text == "Начать SkillPass")
async def start_interview(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    await message.answer("🔄 Инициализация ИИ-асессора... Готовлю первую задачу.", reply_markup=None)
    
    chat = flash_model.start_chat(history=[])
    chat_sessions[tg_id] = chat
    interview_data[tg_id] = {
        "question_num": 1,
        "suspicious_count": 0,
        "transcript": "",
        "last_q_time": time.time()
    }
    
    prompt = (
        "Ты жесткий технический интервьюер AuditCore AI. Роль кандидата: Backend Python Developer. "
        "Задай ОДНУ практическую задачу на код (баг или оптимизация). "
        "Сформулируй кратко. Не давай вариантов ответа."
    )
    
    response = await chat.send_message_async(prompt)
    bot_reply = response.text
    
    interview_data[tg_id]["transcript"] += f"Бот (Q1): {bot_reply}\n"
    await message.answer(f"<b>Вопрос 1 из {MAX_QUESTIONS}:</b>\n\n{bot_reply}", parse_mode=ParseMode.HTML)
    interview_data[tg_id]["last_q_time"] = time.time()
    await state.set_state(InterviewFlow.interviewing)

@router.message(InterviewFlow.interviewing)
async def process_answer(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    user_answer = message.text
    answer_time = time.time()
    
    session = interview_data.get(tg_id)
    if not session: return
        
    time_spent = answer_time - session["last_q_time"]
    q_num = session["question_num"]
    
    session["transcript"] += f"Кандидат (ответ за {int(time_spent)} сек): {user_answer}\n"
    
    if time_spent > TIME_LIMIT:
        await message.answer("❌ Время вышло! (Лимит 90 сек). Ответ не засчитан.")
        session["transcript"] += "[СИСТЕМА]: Кандидат превысил лимит времени.\n"
    elif time_spent < CHEATER_TIME:
        session["suspicious_count"] += 1
        session["transcript"] += "[СИСТЕМА]: ФЛАГ ПОДОЗРЕНИЯ (Аномально быстрый ответ. Вероятно ИИ).\n"
        
        conn = sqlite3.connect('auditcore.db')
        cursor = conn.cursor()
        cursor.execute('UPDATE candidates SET suspicious_count = suspicious_count + 1 WHERE tg_id = ?', (tg_id,))
        conn.commit()
        conn.close()

    if q_num >= MAX_QUESTIONS:
        await finish_interview(message, state, tg_id)
        return

    session["question_num"] += 1
    next_q = session["question_num"]
    
    await message.answer("Обработка ответа...", parse_mode=ParseMode.HTML)
    
    chat = chat_sessions[tg_id]
    prompt = (
        f"Кандидат ответил: '{user_answer}'. "
        "Сгенерируй ОДИН жесткий УТОЧНЯЮЩИЙ вопрос по его коду, чтобы проверить, понимает ли он что написал."
    )
    
    response = await chat.send_message_async(prompt)
    bot_reply = response.text
    
    session["transcript"] += f"Бот (Q{next_q}): {bot_reply}\n"
    await message.answer(f"<b>Вопрос {next_q} из {MAX_QUESTIONS}:</b>\n\n{bot_reply}", parse_mode=ParseMode.HTML)
    session["last_q_time"] = time.time()

async def finish_interview(message: Message, state: FSMContext, tg_id: int):
    await state.clear()
    await message.answer("🏁 Интервью завершено. ИИ-асессор анализирует ваши ответы...")
    
    session = interview_data[tg_id]
    transcript = session["transcript"]
    suspicious = session["suspicious_count"]
    
    analysis_prompt = (
        "Ты Главный HR-аналитик AuditCore AI. Проанализируй диалог технического собеседования.\n\n"
        f"История диалога:\n{transcript}\n\n"
        f"Системные метки подозрительного поведения (быстрые ответы): {suspicious} раз.\n\n"
        "Выдай строгий вердикт в следующем формате:\n"
        "ОЦЕНКА ХАРД-СКИЛЛОВ: [1 до 10]\n"
        "СТАТУС ЧЕСТНОСТИ: [Честный / Подозрительный / Читер] (Если меток подозрения > 0, ставь 'Читер').\n"
        "ОБОСНОВАНИЕ: [2-3 предложения]."
    )
    
    response = await pro_model.generate_content_async(analysis_prompt)
    verdict = response.text
    
    conn = sqlite3.connect('auditcore.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE candidates SET transcript = ?, status = 'Завершено' WHERE tg_id = ?", (transcript, tg_id))
    cursor.execute('SELECT name, phone, github, exp FROM candidates WHERE tg_id = ?', (tg_id,))
    user_info = cursor.fetchone()
    conn.commit()
    conn.close()
    
    await message.answer("✅ Результаты зафиксированы и переданы HR-департаменту (AuditCore AI).")
    
    admin_report = (
        f"🚨 <b>Анализ кандидата (AuditCore MVP)</b> 🚨\n\n"
        f"👤 Имя: {user_info[0]}\n"
        f"📞 Телефон: {user_info[1]}\n"
        f"💻 GitHub: {user_info[2]}\n"
        f"⏱ Опыт: {user_info[3]} лет\n\n"
        f"🤖 <b>ВЕРДИКТ ИИ:</b>\n{verdict}\n\n"
        f"⚠️ Количество флагов на скорость (до 15 сек): <b>{suspicious}</b>"
    )
    
    try:
        await bot.send_message(ADMIN_ID, admin_report, parse_mode=ParseMode.HTML)
    except Exception as e:
        logging.error(f"Ошибка отправки админу: {e}")

async def main():
    init_db()
    dp.include_router(router)
    print("Бот успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
