import asyncio
from datetime import datetime
from aiogram import Bot, Dispatcher, html, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from config import settings
import database as db
import services as ai

bot = Bot(token=settings.TELEGRAM_TOKEN)
dp = Dispatcher()

class InterviewForm(StatesGroup):
    choosing_language = State()
    entering_name = State()
    entering_email = State()
    choosing_role = State()
    in_interview = State()

# Тексты локализации UI
LOCALIZATION = {
    "ru": {
        "welcome": "Приветствуем в AuditCore AI SkillPass! Выберите язык для прохождения интервью:",
        "ask_name": "Введите ваши Имя и Фамилию:",
        "ask_email": "Введите ваш контактный Email:",
        "ask_role": "Выберите ИТ-направление, на которое вы претендуете:",
        "start_ready": "Вы зарегистрированы! Нажмите кнопку ниже, чтобы запустить адаптивную сессию. Помните: на каждый ответ ИИ выдает ограниченное время!",
        "btn_start": "🚀 Начать SkillPass интервью",
        "timeout": "⏰ Время вышло! Этот раунд засчитан как пропуск. Переходим к следующему вопросу ИИ.",
        "thanks": "🎉 Интервью успешно завершено! Ваши результаты обработаны и отправлены нанимающему менеджеру. Спасибо!"
    },
    "en": {
        "welcome": "Welcome to AuditCore AI SkillPass! Choose your interview language:",
        "ask_name": "Enter your Full Name:",
        "ask_email": "Enter your contact Email:",
        "ask_role": "Select the IT specialization you are applying for:",
        "start_ready": "Registration complete! Click the button below to start your adaptive session. Note: each question has a strict AI-defined timeout!",
        "btn_start": "🚀 Start SkillPass Interview",
        "timeout": "⏰ Time's up! This turn is marked as empty. Moving to the next AI question.",
        "thanks": "🎉 Interview finished successfully! Your results have been evaluated and delivered to the hiring manager. Thank you!"
    }
}

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Русский 🇷🇺", callback_data="lang_ru"),
         InlineKeyboardButton(text="English 🇬🇧", callback_data="lang_en")]
    ])
    await message.answer(LOCALIZATION["ru"]["welcome"], reply_markup=kb)
    await state.set_state(InterviewForm.choosing_language)

@dp.callback_query(F.data.startswith("lang_"), InterviewForm.choosing_language)
async def lang_selected(callback: CallbackQuery, state: FSMContext):
    lang = callback.data.split("_")[1]
    await state.update_data(language=lang)
    await callback.message.delete()
    await callback.message.answer(LOCALIZATION[lang]["ask_name"])
    await state.set_state(InterviewForm.entering_name)

@dp.message(InterviewForm.entering_name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(fullname=message.text)
    data = await state.get_data()
    lang = data["language"]
    await message.answer(LOCALIZATION[lang]["ask_email"])
    await state.set_state(InterviewForm.entering_email)

@dp.message(InterviewForm.entering_email)
async def process_email(message: Message, state: FSMContext):
    await state.update_data(email=message.text)
    data = await state.get_data()
    lang = data["language"]
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Backend Developer (Python)", callback_data="role_Backend Developer")],
        [InlineKeyboardButton(text="Frontend Developer (JavaScript)", callback_data="role_Frontend Developer")],
        [InlineKeyboardButton(text="Data Analyst (SQL/Python)", callback_data="role_Data Analyst")]
    ])
    await message.answer(LOCALIZATION[lang]["ask_role"], reply_markup=kb)
    await state.set_state(InterviewForm.choosing_role)

@dp.callback_query(F.data.startswith("role_"), InterviewForm.choosing_role)
async def role_selected(callback: CallbackQuery, state: FSMContext):
    role = callback.data.split("_")[1]
    await state.update_data(role=role)
    data = await state.get_data()
    lang = data["language"]
    
    await db.save_candidate(callback.from_user.id, data["fullname"], data["email"], role, lang)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=LOCALIZATION[lang]["btn_start"], callback_data="start_session")]
    ])
    await callback.message.delete()
    await callback.message.answer(LOCALIZATION[lang]["start_ready"], reply_markup=kb)

# Фоновый таймер дефицита времени
async def interview_timer(tg_id: int, state: FSMContext, turn_idx: int, delay: int):
    await asyncio.sleep(delay)
    current_state = await state.get_state()
    if current_state == InterviewForm.in_interview:
        state_data = await state.get_data()
        if state_data.get("turn_idx") == turn_idx:
            # Юзер не успел. Форсируем таймаут
            lang = state_data["language"]
            await bot.send_message(tg_id, LOCALIZATION[lang]["timeout"])
            await next_interview_turn(tg_id, "TIMEOUT_EVENT_NO_ANSWER", state)

@dp.callback_query(F.data == "start_session")
async def trigger_interview(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await next_interview_turn(callback.from_user.id, "START_INTERVIEW", state)

async def next_interview_turn(tg_id: int, last_answer: str, state: FSMContext):
    data = await state.get_data()
    lang = data["language"]
    role = data["role"]
    
    # Сохраняем предыдущий шаг в историю, если это не старт
    if last_answer != "START_INTERVIEW":
        last_q = data.get("current_question", "")
        await db.log_turn(tg_id, last_q, last_answer)
    
    # Получаем всю историю текущей сессии
    history_text = await db.get_transcript(tg_id)
    if last_answer == "TIMEOUT_EVENT_NO_ANSWER":
        history_text += f"System: Candidate ran out of time on the question below.\n"

    # Обращаемся к Groq
    ai_turn = ai.get_ai_question(role, lang, history_text)
    
    if ai_turn.is_interview_finished:
        await bot.send_message(tg_id, LOCALIZATION[lang]["thanks"])
        # Сборка финального отчета на русском через Groq Llama 3.1
        final_transcript = await db.get_transcript(tg_id)
        report = ai.generate_final_report_ru(data["fullname"], role, final_transcript)
        
        # Отправка отчета на monkifani@gmail.com
        ai.send_email_report(data["fullname"], role, report)
        await state.clear()
        return

    # Обновляем индекс раунда для работы таймера
    new_turn_idx = data.get("turn_idx", 0) + 1
    await state.update_data(current_question=ai_turn.question_text, turn_idx=new_turn_idx)
    
    # Отправляем вопрос и запускаем серверный таймер контроля времени
    time_label = "сек." if lang == "ru" else "sec."
    msg_text = f"{ai_turn.question_text}\n\n⏱ [{ai_turn.allowed_time_seconds} {time_label}]"
    await bot.send_message(tg_id, msg_text)
    
    await state.set_state(InterviewForm.in_interview)
    asyncio.create_task(interview_timer(tg_id, state, new_turn_idx, ai_turn.allowed_time_seconds))

@dp.message(InterviewForm.in_interview)
async def handle_answer(message: Message, state: FSMContext):
    # Кандидат успел ответить. Переходим к следующему шагу
    await next_interview_turn(message.from_user.id, message.text, state)

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != settings.ADMIN_ID:
        return
    candidates = await db.get_all_candidates()
    if not candidates:
        await message.answer("База данных кандидатов пуста.")
        return
    
    response = "📊 **Список участников сессий:**\n\n"
    for c in candidates:
        response += f"👤 {c[1]} ({c[2]})\nРоль: {c[3]} | Статус: {c[4]}\n`ID: {c[0]}`\n\n"
    await message.answer(response)

async def main():
    await db.init_db()
    print("База данных успешно инициализирована.")
    await dp.start_polling(bot)

import threading
from http.server import SimpleHTTPRequestHandler, HTTPServer

# Мини веб-сервер для капризного Render
def run_dummy_server():
    # Render автоматически передает порт в переменную окружения PORT
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    print(f"Запущен заглушка-сервер на порту {port}")
    server.serve_forever()

async def main():
    import os
    await db.init_db()
    print("База данных успешно инициализирована.")
    
    # Запускаем веб-сервер в отдельном потоке, чтобы Render успокоился
    threading.Thread(target=run_dummy_server, daemon=True).start()
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

  
        
