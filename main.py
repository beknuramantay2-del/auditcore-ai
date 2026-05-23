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
    ReplyKeyboardRemove, BufferedInputFile
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.enums import ParseMode

from config import TELEGRAM_TOKEN, ADMIN_ID, PORT, MAX_TURNS, CHEATER_SPEED_THRESHOLD
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

# Хранилище таймеров и истории диалогов
active_timers: dict[int, asyncio.Task] = {}
chat_histories: dict[int, list] = {}  # tg_id -> [{"role": ..., "content": ...}]

# ───────────────────────────────────────────────
# FSM
# ───────────────────────────────────────────────

class Flow(StatesGroup):
    choosing_language = State()
    entering_name     = State()
    entering_email    = State()
    choosing_role     = State()
    ready_to_start    = State()
    interviewing      = State()

# ───────────────────────────────────────────────
# ТЕКСТЫ
# ───────────────────────────────────────────────

TEXTS = {
    "ru": {
        "welcome":          "👋 Добро пожаловать в <b>AuditCore AI</b>!\n\nВыберите язык интервью:",
        "enter_name":       "✏️ Введите ваше <b>полное имя (ФИО)</b>:",
        "enter_email":      "📧 Введите ваш <b>Email</b>:",
        "choose_role":      "💼 Выберите роль, на которую претендуете:",
        "onboarding":       (
            "✅ <b>Регистрация завершена!</b>\n\n"
            "📋 <b>Правила SkillPass:</b>\n"
            "• ИИ-асессор задаст вам <b>4 практических вопроса</b>\n"
            "• Каждый вопрос адаптируется под ваш предыдущий ответ\n"
            "• На каждый вопрос — строгий таймер (<b>45–120 сек</b>)\n"
            "• Аномально быстрые ответы автоматически фиксируются\n"
            "• По окончании HR получит полный аналитический отчёт\n\n"
            "Когда будете готовы — нажмите кнопку ниже:"
        ),
        "start_btn":        "🚀 Начать SkillPass",
        "thinking":         "⏳ ИИ-асессор анализирует и готовит следующий вопрос...",
        "q_header":         "❓ <b>Вопрос {turn} из {max}</b>\n⏱ Время на ответ: <b>{time} сек</b>\n\n{question}",
        "timeout":          "⏰ <b>Время вышло!</b> Ответ не засчитан. Перехожу к следующему вопросу...",
        "processing":       "🔄 Обрабатываю ваш ответ...",
        "speed_flag":       "⚡ Скорость ответа зафиксирована системой мониторинга.",
        "finished":         (
            "🏁 <b>Интервью завершено!</b>\n\n"
            "Ваши результаты анализируются ИИ-системой.\n"
            "Полный отчёт уже направлен в HR-департамент.\n\n"
            "Спасибо за участие в AuditCore SkillPass!"
        ),
    },
    "en": {
        "welcome":          "👋 Welcome to <b>AuditCore AI</b>!\n\nPlease choose your interview language:",
        "enter_name":       "✏️ Enter your <b>Full Name</b>:",
        "enter_email":      "📧 Enter your <b>Email</b>:",
        "choose_role":      "💼 Select the role you are applying for:",
        "onboarding":       (
            "✅ <b>Registration complete!</b>\n\n"
            "📋 <b>SkillPass Rules:</b>\n"
            "• The AI assessor will ask you <b>4 practical questions</b>\n"
            "• Each question adapts based on your previous answer\n"
            "• Each question has a strict timer (<b>45–120 sec</b>)\n"
            "• Suspiciously fast answers are automatically flagged\n"
            "• After the session, HR receives a full analytical report\n\n"
            "When ready — press the button below:"
        ),
        "start_btn":        "🚀 Start SkillPass",
        "thinking":         "⏳ AI assessor is analyzing and preparing the next question...",
        "q_header":         "❓ <b>Question {turn} of {max}</b>\n⏱ Time allowed: <b>{time} sec</b>\n\n{question}",
        "timeout":          "⏰ <b>Time is up!</b> No answer recorded. Moving to the next question...",
        "processing":       "🔄 Processing your answer...",
        "speed_flag":       "⚡ Your response speed has been logged by the monitoring system.",
        "finished":         (
            "🏁 <b>Interview complete!</b>\n\n"
            "Your results are being analyzed by the AI system.\n"
            "The full report has been sent to HR.\n\n"
            "Thank you for completing AuditCore SkillPass!"
        ),
    }
}

def t(lang: str, key: str, **kwargs) -> str:
    text = TEXTS.get(lang, TEXTS["ru"]).get(key, "")
    return text.format(**kwargs) if kwargs else text

# ───────────────────────────────────────────────
# КЛАВИАТУРЫ
# ───────────────────────────────────────────────

def lang_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru"),
        InlineKeyboardButton(text="🇬🇧 English",  callback_data="lang_en"),
    ]])

def role_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Backend Developer",  callback_data="role_Backend Developer")],
        [InlineKeyboardButton(text="🎨 Frontend Developer", callback_data="role_Frontend Developer")],
        [InlineKeyboardButton(text="📊 Data Analyst",       callback_data="role_Data Analyst")],
    ])

def start_kb(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t(lang, "start_btn"))]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

# ───────────────────────────────────────────────
# ТАЙМЕР И ЛОГИКА ИНТЕРВЬЮ
# ───────────────────────────────────────────────

async def cancel_timer(tg_id: int):
    task = active_timers.pop(tg_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

async def finish_interview(tg_id: int, chat_id: int, state: FSMContext):
    await cancel_timer(tg_id)
    chat_histories.pop(tg_id, None)
    await state.clear()

    candidate = await get_candidate(tg_id)
    lang = candidate.get("language", "ru")

    await bot.send_message(chat_id, t(lang, "finished"), parse_mode=ParseMode.HTML)

    # Генерируем финальный отчёт
    report = await generate_final_report(candidate)
    await upsert_candidate(tg_id, final_verdict=report, status="completed")

    # Email
    await send_email_report(candidate, report)

    # Дублируем в Telegram администратору
    short_report = report[:3800] if len(report) > 3800 else report
    admin_msg = (
        f"🚨 <b>Новый отчёт AuditCore AI</b>\n\n"
        f"👤 <b>{candidate.get('name', '?')}</b> | {candidate.get('role', '?')}\n"
        f"📧 {candidate.get('email', '?')}\n"
        f"⚠️ Флагов скорости: <b>{candidate.get('suspicious_count', 0)}</b> | "
        f"Таймаутов: <b>{candidate.get('timeout_count', 0)}</b>\n\n"
        f"{short_report}"
    )
    try:
        await bot.send_message(ADMIN_ID, admin_msg, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Ошибка отправки отчёта админу: {e}")


async def run_turn(
    tg_id: int,
    chat_id: int,
    state: FSMContext,
    previous_answer: str = "",
    is_timeout: bool = False
):
    candidate = await get_candidate(tg_id)
    lang  = candidate.get("language", "ru")
    role  = candidate.get("role", "Backend Developer")
    turn  = candidate.get("turn_count", 0) + 1

    if turn > MAX_TURNS:
        await finish_interview(tg_id, chat_id, state)
        return

    await upsert_candidate(tg_id, turn_count=turn)

    # Обновляем историю диалога для GPT
    history = chat_histories.setdefault(tg_id, [])

    if is_timeout:
        history.append({
            "role": "user",
            "content": "[ТАЙМАУТ: кандидат не успел ответить. Дай новый, чуть более простой вопрос.]"
        })
    elif previous_answer:
        history.append({
            "role": "user",
            "content": previous_answer
        })

    await bot.send_message(chat_id, t(lang, "thinking"))

    # Запрашиваем вопрос у GPT с полным контекстом истории
    q_data = await generate_question(
        role=role,
        language=lang,
        turn=turn,
        chat_history=history,
    )

    question_text = q_data["question_text"]
    allowed_time  = q_data["allowed_time_seconds"]

    # Добавляем вопрос бота в историю
    history.append({"role": "assistant", "content": question_text})

    # Логируем в транскрипт БД
    await append_transcript(
        tg_id,
        f"[ВОПРОС {turn}/{MAX_TURNS}] (лимит {allowed_time}с):\n{question_text}"
    )

    await bot.send_message(
        chat_id,
        t(lang, "q_header", turn=turn, max=MAX_TURNS,
          time=allowed_time, question=question_text),
        parse_mode=ParseMode.HTML
    )

    # Сохраняем время отправки вопроса
    await state.update_data(q_sent_at=time.time(), allowed_time=allowed_time)
    await state.set_state(Flow.interviewing)

    # Запускаем фоновый таймер
    await cancel_timer(tg_id)
    task = asyncio.create_task(
        _timeout_task(tg_id, chat_id, state, allowed_time, lang)
    )
    active_timers[tg_id] = task


async def _timeout_task(tg_id: int, chat_id: int, state: FSMContext,
                         allowed_time: int, lang: str):
    await asyncio.sleep(allowed_time)

    # Проверяем что кандидат всё ещё в состоянии интервью
    current = await state.get_state()
    if current != Flow.interviewing.state:
        return

    logger.info(f"TIMEOUT for tg_id={tg_id}")

    await append_transcript(
        tg_id,
        f"[СИСТЕМА] ⏰ ТАЙМАУТ — кандидат не ответил за {allowed_time} секунд."
    )

    candidate = await get_candidate(tg_id)
    new_timeouts = candidate.get("timeout_count", 0) + 1
    await upsert_candidate(tg_id, timeout_count=new_timeouts)

    await bot.send_message(chat_id, t(lang, "timeout"), parse_mode=ParseMode.HTML)

    candidate = await get_candidate(tg_id)
    if candidate.get("turn_count", 0) >= MAX_TURNS:
        await finish_interview(tg_id, chat_id, state)
    else:
        await run_turn(tg_id, chat_id, state, is_timeout=True)

# ───────────────────────────────────────────────
# ХЭНДЛЕРЫ
# ───────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    await state.clear()
    await cancel_timer(tg_id)
    chat_histories.pop(tg_id, None)

    await message.answer(
        t("ru", "welcome"),
        reply_markup=lang_kb(),
        parse_mode=ParseMode.HTML
    )
    await state.set_state(Flow.choosing_language)


@router.callback_query(Flow.choosing_language, F.data.startswith("lang_"))
async def cb_language(call: CallbackQuery, state: FSMContext):
    lang  = call.data.split("_")[1]
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
        reply_markup=role_kb(),
        parse_mode=ParseMode.HTML
    )
    await state.set_state(Flow.choosing_role)


@router.callback_query(Flow.choosing_role, F.data.startswith("role_"))
async def cb_role(call: CallbackQuery, state: FSMContext):
    role  = call.data[5:]
    tg_id = call.from_user.id
    candidate = await get_candidate(tg_id)
    lang = candidate.get("language", "ru") if candidate else "ru"

    await upsert_candidate(tg_id, role=role)
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(
        t(lang, "onboarding"),
        reply_markup=start_kb(lang),
        parse_mode=ParseMode.HTML
    )
    await state.set_state(Flow.ready_to_start)
    await call.answer()


@router.message(Flow.ready_to_start)
async def handle_start_btn(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    candidate = await get_candidate(tg_id)
    lang = candidate.get("language", "ru") if candidate else "ru"

    if message.text != t(lang, "start_btn"):
        return

    await message.answer("▶️", reply_markup=ReplyKeyboardRemove())
    await run_turn(tg_id, message.chat.id, state)


@router.message(Flow.interviewing)
async def handle_answer(message: Message, state: FSMContext):
    tg_id       = message.from_user.id
    answer_time = time.time()

    # Отменяем таймер т.к. кандидат ответил вовремя
    await cancel_timer(tg_id)

    fsm_data     = await state.get_data()
    q_sent_at    = fsm_data.get("q_sent_at", answer_time)
    allowed_time = fsm_data.get("allowed_time", 90)
    delta        = round(answer_time - q_sent_at, 1)

    candidate = await get_candidate(tg_id)
    lang  = candidate.get("language", "ru") if candidate else "ru"
    turn  = candidate.get("turn_count", 1)

    # Логируем ответ
    await append_transcript(
        tg_id,
        f"[ОТВЕТ {turn}/{MAX_TURNS}] (за {delta}с из {allowed_time}с):\n{message.text}"
    )

    # Проверяем на подозрительную скорость
    if delta < CHEATER_SPEED_THRESHOLD:
        await append_transcript(
            tg_id,
            f"[СИСТЕМА] ⚠️ ФЛАГ СКОРОСТИ: ответ за {delta}с "
            f"(порог {CHEATER_SPEED_THRESHOLD}с) — возможное использование ИИ"
        )
        new_suspicious = candidate.get("suspicious_count", 0) + 1
        await upsert_candidate(tg_id, suspicious_count=new_suspicious)
        await message.answer(t(lang, "speed_flag"))

    await message.answer(t(lang, "processing"))

    # Завершаем или продолжаем
    if turn >= MAX_TURNS:
        await finish_interview(tg_id, message.chat.id, state)
    else:
        await run_turn(
            tg_id, message.chat.id, state,
            previous_answer=message.text
        )


# ───────────────────────────────────────────────
# ADMIN
# ───────────────────────────────────────────────

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Доступ запрещён.")
        return

    candidates = await get_all_candidates()
    if not candidates:
        await message.answer("📭 База данных пуста.")
        return

    # Текстовая сводка
    summary = "📊 <b>AuditCore AI — Все кандидаты</b>\n" + "─" * 30 + "\n"
    for c in candidates:
        verdict_line = ""
        if c.get("final_verdict"):
            lines = c["final_verdict"].split("\n")
            for line in lines:
                if "ВЕРДИКТ" in line or "VERDICT" in line:
                    verdict_line = line.strip()
                    break
        summary += (
            f"\n👤 <b>{c.get('name','?')}</b> | {c.get('role','?')}\n"
            f"   📧 {c.get('email','?')}\n"
            f"   🏳️ Язык: {c.get('language','?')} | "
            f"Статус: {c.get('status','?')}\n"
            f"   ⚠️ Флаги: {c.get('suspicious_count',0)} | "
            f"Таймауты: {c.get('timeout_count',0)}\n"
            f"   {verdict_line}\n"
        )

    # CSV
    output = io.StringIO()
    fieldnames = [
        "id", "tg_id", "name", "email", "role", "language",
        "turn_count", "suspicious_count", "timeout_count",
        "status", "created_at"
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for c in candidates:
        writer.writerow({k: c.get(k, "") for k in fieldnames})

    output.seek(0)
    csv_bytes = output.getvalue().encode("utf-8-sig")  # utf-8-sig для Excel

    await message.answer(summary, parse_mode=ParseMode.HTML)
    await message.answer_document(
        BufferedInputFile(csv_bytes, filename="auditcore_candidates.csv"),
        caption="📁 CSV-экспорт (открывается в Excel)"
    )


# ───────────────────────────────────────────────
# ВЕБ-СЕРВЕР ДЛЯ RENDER
# ───────────────────────────────────────────────

async def handle_web(request):
    return web.Response(text="AuditCore AI ✅ Running")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_web)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server on port {PORT}")


# ───────────────────────────────────────────────
# ЗАПУСК
# ───────────────────────────────────────────────

async def main():
    await init_db()
    dp.include_router(router)
    await start_web_server()
    logger.info("AuditCore AI Bot started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
  
      

   


  
  
        
