import asyncio
import logging
import time
import io
import csv

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    ReplyKeyboardRemove
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.enums import ParseMode

from config import TELEGRAM_TOKEN, ADMIN_ID, PORT, MAX_TURNS
from database import (
    init_db, insert_candidate, upsert_candidate,
    get_candidate, get_all_candidates, append_transcript
)
from services import generate_question, generate_final_report, send_email_report

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
router = Router()

# Хранилище активных таймеров { tg_id: asyncio.Task }
active_timers: dict[int, asyncio.Task] = {}

# ───────────────────────────────────────────────
# FSM СОСТОЯНИЯ
# ───────────────────────────────────────────────

class Flow(StatesGroup):
    choosing_language = State()
    entering_name     = State()
    entering_email    = State()
    choosing_role     = State()
    ready_to_start    = State()
    interviewing      = State()

# ───────────────────────────────────────────────
# ТЕКСТЫ НА ДВУХ ЯЗЫКАХ
# ───────────────────────────────────────────────

TEXTS = {
    "ru": {
        "welcome":       "👋 Добро пожаловать в <b>AuditCore AI</b>!\nВыберите язык интервью:",
        "enter_name":    "Введите ваше <b>полное имя (ФИО)</b>:",
        "enter_email":   "Введите ваш <b>Email</b>:",
        "choose_role":   "Выберите роль, на которую претендуете:",
        "onboarding":    (
            "✅ Регистрация завершена!\n\n"
            "⚠️ <b>Правила SkillPass:</b>\n"
            "• ИИ-асессор задаёт 4 практических вопроса\n"
            "• На каждый вопрос — строгий таймер (45–120 сек)\n"
            "• Слишком быстрые ответы фиксируются как подозрительные\n"
            "• По окончании HR получает полный аналитический отчёт\n\n"
            "Готовы?"
        ),
        "start_btn":     "🚀 Начать SkillPass",
        "thinking":      "⏳ ИИ-асессор готовит вопрос...",
        "question_header": "❓ <b>Вопрос {turn} из {max}:</b>\n⏱ Время на ответ: <b>{time} сек</b>\n\n{question}",
        "timeout_msg":   "⏰ <b>Время вышло!</b> Ответ не засчитан. Перехожу к следующему вопросу...",
        "processing":    "🔄 Обрабатываю ваш ответ...",
        "finished":      "🏁 <b>Интервью завершено!</b>\n\nВаши результаты анализируются ИИ. Отчёт уже направлен в HR-департамент.\n\nСпасибо за участие в SkillPass!",
        "cheater_flag":  "⚡ Скорость ответа зафиксирована системой.",
    },
    "en": {
        "welcome":       "👋 Welcome to <b>AuditCore AI</b>!\nPlease choose your interview language:",
        "enter_name":    "Enter your <b>Full Name</b>:",
        "enter_email":   "Enter your <b>Email</b>:",
        "choose_role":   "Select the role you are applying for:",
        "onboarding":    (
            "✅ Registration complete!\n\n"
            "⚠️ <b>SkillPass Rules:</b>\n"
            "• The AI assessor will ask 4 practical questions\n"
            "• Each question has a strict timer (45–120 sec)\n"
            "• Suspiciously fast answers are flagged automatically\n"
            "• After the session, HR receives a full analytical report\n\n"
            "Ready?"
        ),
        "start_btn":     "🚀 Start SkillPass",
        "thinking":      "⏳ AI assessor is preparing your question...",
        "question_header": "❓ <b>Question {turn} of {max}:</b>\n⏱ Time allowed: <b>{time} sec</b>\n\n{question}",
        "timeout_msg":   "⏰ <b>Time is up!</b> No answer recorded. Moving to the next question...",
        "processing":    "🔄 Processing your answer...",
        "finished":      "🏁 <b>Interview complete!</b>\n\nYour results are being analyzed. The report has been sent to HR.\n\nThank you for completing SkillPass!",
        "cheater_flag":  "⚡ Response speed has been logged by the system.",
    }
}

def t(lang: str, key: str) -> str:
    return TEXTS.get(lang, TEXTS["ru"]).get(key, "")

# ───────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ───────────────────────────────────────────────

def lang_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru"),
        InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en"),
    ]])

def role_keyboard(lang: str) -> InlineKeyboardMarkup:
    roles = ["Backend Developer", "Frontend Developer", "Data Analyst"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=r, callback_data=f"role_{r}")] for r in roles
    ])

def start_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t(lang, "start_btn"))]],
        resize_keyboard=True
    )

async def cancel_timer(tg_id: int):
    task = active_timers.pop(tg_id, None)
    if task and not task.done():
        task.cancel()

async def finish_interview(tg_id: int, chat_id: int, state: FSMContext):
    """Финализация интервью: генерация отчёта и отправка email."""
    await cancel_timer(tg_id)
    await state.clear()

    candidate = await get_candidate(tg_id)
    lang = candidate.get("language", "ru")

    await bot.send_message(chat_id, t(lang, "finished"), parse_mode=ParseMode.HTML)

    # Генерируем отчёт
    report = await generate_final_report(candidate)

    # Сохраняем вердикт в БД
    await upsert_candidate(tg_id, final_verdict=report, status="completed")

    # Отправляем email
    await send_email_report(candidate, report)

    # Дублируем отчёт администратору в Telegram
    admin_msg = (
        f"🚨 <b>Новый отчёт AuditCore AI</b>\n\n"
        f"👤 {candidate.get('name')} | {candidate.get('role')}\n"
        f"📧 {candidate.get('email')}\n"
        f"⚠️ Флагов скорости: {candidate.get('suspicious_count', 0)} | "
        f"Таймаутов: {candidate.get('timeout_count', 0)}\n\n"
        f"{report[:3500]}"
    )
    try:
        await bot.send_message(ADMIN_ID, admin_msg, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Ошибка отправки отчёта админу: {e}")


async def run_turn(tg_id: int, chat_id: int, state: FSMContext,
                   previous_answer: str = "", timeout: bool = False):
    """Один цикл интервью: запрос вопроса у Gemini → отправка → запуск таймера."""
    candidate = await get_candidate(tg_id)
    lang = candidate.get("language", "ru")
    role = candidate.get("role", "Backend Developer")
    turn = candidate.get("turn_count", 0) + 1
    transcript = candidate.get("transcript", "")

    if turn > MAX_TURNS:
        await finish_interview(tg_id, chat_id, state)
        return

    await upsert_candidate(tg_id, turn_count=turn)
    await bot.send_message(chat_id, t(lang, "thinking"))

    q_data = await generate_question(
        role=role,
        language=lang,
        turn=turn,
        previous_answer=previous_answer,
        timeout=timeout,
        transcript=transcript
    )

    question_text = q_data["question_text"]
    allowed_time  = q_data["allowed_time_seconds"]

    # Логируем в транскрипт
    await append_transcript(
        tg_id,
        f"[Q{turn}] БОТ (лимит {allowed_time}с): {question_text}"
    )

    header = t(lang, "question_header").format(
        turn=turn, max=MAX_TURNS,
        time=allowed_time, question=question_text
    )
    await bot.send_message(chat_id, header, parse_mode=ParseMode.HTML)

    # Сохраняем время отправки вопроса в FSM
    await state.update_data(q_sent_at=time.time(), allowed_time=allowed_time)
    await state.set_state(Flow.interviewing)

    # Запускаем таймер
    await cancel_timer(tg_id)
    task = asyncio.create_task(
        _timeout_task(tg_id, chat_id, state, allowed_time, lang)
    )
    active_timers[tg_id] = task


async def _timeout_task(tg_id: int, chat_id: int, state: FSMContext,
                         allowed_time: int, lang: str):
    """Фоновый таймер. Срабатывает если кандидат не ответил вовремя."""
    await asyncio.sleep(allowed_time)

    current_state = await state.get_state()
    if current_state != Flow.interviewing.state:
        return

    await append_transcript(tg_id, "[СИСТЕМА] ТАЙМАУТ — кандидат не ответил вовремя.")
    await upsert_candidate(tg_id,
        timeout_count=(await get_candidate(tg_id)).get("timeout_count", 0) + 1
    )

    await bot.send_message(chat_id, t(lang, "timeout_msg"), parse_mode=ParseMode.HTML)

    candidate = await get_candidate(tg_id)
    if candidate.get("turn_count", 0) >= MAX_TURNS:
        await finish_interview(tg_id, chat_id, state)
    else:
        await run_turn(tg_id, chat_id, state, previous_answer="", timeout=True)

# ───────────────────────────────────────────────
# ХЭНДЛЕРЫ
# ───────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await cancel_timer(message.from_user.id)
    await message.answer(
        t("ru", "welcome"),
        reply_markup=lang_keyboard(),
        parse_mode=ParseMode.HTML
    )
    await state.set_state(Flow.choosing_language)


@router.callback_query(Flow.choosing_language, F.data.startswith("lang_"))
async def cb_language(call: CallbackQuery, state: FSMContext):
    lang = call.data.split("_")[1]
    tg_id = call.from_user.id

    await insert_candidate(tg_id, lang)
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(t(lang, "enter_name"), parse_mode=ParseMode.HTML)
    await state.set_state(Flow.entering_name)
    await call.answer()


@router.message(Flow.entering_name)
async def process_name(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    candidate = await get_candidate(tg_id)
    lang = candidate.get("language", "ru") if candidate else "ru"

    await upsert_candidate(tg_id, name=message.text.strip())
    await message.answer(t(lang, "enter_email"), parse_mode=ParseMode.HTML)
    await state.set_state(Flow.entering_email)


@router.message(Flow.entering_email)
async def process_email(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    candidate = await get_candidate(tg_id)
    lang = candidate.get("language", "ru") if candidate else "ru"

    await upsert_candidate(tg_id, email=message.text.strip())
    await message.answer(
        t(lang, "choose_role"),
        reply_markup=role_keyboard(lang),
        parse_mode=ParseMode.HTML
    )
    await state.set_state(Flow.choosing_role)


@router.callback_query(Flow.choosing_role, F.data.startswith("role_"))
async def cb_role(call: CallbackQuery, state: FSMContext):
    role = call.data[5:]
    tg_id = call.from_user.id
    candidate = await get_candidate(tg_id)
    lang = candidate.get("language", "ru") if candidate else "ru"

    await upsert_candidate(tg_id, role=role)
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(
        t(lang, "onboarding"),
        reply_markup=start_keyboard(lang),
        parse_mode=ParseMode.HTML
    )
    await state.set_state(Flow.ready_to_start)
    await call.answer()


@router.message(Flow.ready_to_start)
async def handle_start_skillpass(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    candidate = await get_candidate(tg_id)
    lang = candidate.get("language", "ru") if candidate else "ru"

    btn = t(lang, "start_btn")
    if message.text != btn:
        return

    await message.answer("...", reply_markup=ReplyKeyboardRemove())
    await run_turn(tg_id, message.chat.id, state)


@router.message(Flow.interviewing)
async def handle_answer(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    answer_time = time.time()

    await cancel_timer(tg_id)

    fsm_data = await state.get_data()
    q_sent_at    = fsm_data.get("q_sent_at", answer_time)
    allowed_time = fsm_data.get("allowed_time", 90)
    delta        = round(answer_time - q_sent_at, 1)

    candidate = await get_candidate(tg_id)
    lang = candidate.get("language", "ru") if candidate else "ru"
    turn = candidate.get("turn_count", 1)

    # Логируем ответ
    log_line = f"[A{turn}] КАНДИДАТ ({delta}с): {message.text}"
    await append_transcript(tg_id, log_line)

    # Проверяем на подозрительную скорость
    from config import CHEATER_SPEED_THRESHOLD
    if delta < CHEATER_SPEED_THRESHOLD:
        await append_transcript(
            tg_id,
            f"[СИСТЕМА] ⚠️ ФЛАГ СКОРОСТИ: ответ за {delta}с (порог {CHEATER_SPEED_THRESHOLD}с)"
        )
        new_suspicious = candidate.get("suspicious_count", 0) + 1
        await upsert_candidate(tg_id, suspicious_count=new_suspicious)
        await message.answer(t(lang, "cheater_flag"))

    await message.answer(t(lang, "processing"))

    # Проверяем, завершено ли интервью
    if turn >= MAX_TURNS:
        await finish_interview(tg_id, message.chat.id, state)
        return

    await run_turn(
        tg_id, message.chat.id, state,
        previous_answer=message.text
    )


# ───────────────────────────────────────────────
# ADMIN КОМАНДА
# ───────────────────────────────────────────────

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Доступ запрещён.")
        return

    candidates = await get_all_candidates()
    if not candidates:
        await message.answer("База данных пуста.")
        return

    # Генерируем CSV в памяти
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["id","tg_id","name","email","role","language",
                    "turn_count","suspicious_count","timeout_count",
                    "status","created_at"]
    )
    writer.writeheader()
    for c in candidates:
        writer.writerow({k: c.get(k, "") for k in writer.fieldnames})

    output.seek(0)
    csv_bytes = output.getvalue().encode("utf-8")

    # Краткая сводка
    summary = "📊 <b>AuditCore AI — База кандидатов</b>\n\n"
    for c in candidates:
        summary += (
            f"👤 {c.get('name','?')} | {c.get('role','?')} | "
            f"Статус: {c.get('status','?')} | "
            f"Флаги: {c.get('suspicious_count',0)} | "
            f"Таймауты: {c.get('timeout_count',0)}\n"
        )

    await message.answer(summary, parse_mode=ParseMode.HTML)

    from aiogram.types import BufferedInputFile
    await message.answer_document(
        BufferedInputFile(csv_bytes, filename="candidates.csv"),
        caption="📁 Полный CSV-экспорт"
    )


# ───────────────────────────────────────────────
# ВЕБ-СЕРВЕР ДЛЯ RENDER (keep-alive)
# ───────────────────────────────────────────────

async def handle_web(request):
    return web.Response(text="AuditCore AI is running ✅")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_web)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}")


# ───────────────────────────────────────────────
# ТОЧКА ВХОДА
# ───────────────────────────────────────────────

async def main():
    await init_db()
    dp.include_router(router)
    await start_web_server()
    logger.info("AuditCore AI Bot started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
