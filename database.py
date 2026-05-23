import aiosqlite
from config import settings

async def init_db():
    async with aiosqlite.connect(settings.DATABASE_PATH) as db:
        # Таблица кандидатов и их сессий
        await db.execute("""
            CREATE TABLE IF NOT EXISTS candidates (
                tg_id INTEGER PRIMARY KEY,
                fullname TEXT,
                email TEXT,
                role TEXT,
                language TEXT,
                status TEXT DEFAULT 'in_progress',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Таблица логов диалога
        await db.execute("""
            CREATE TABLE IF NOT EXISTS interview_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER,
                role_prompt TEXT,
                user_answer TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

async def save_candidate(tg_id: int, fullname: str, email: str, role: str, language: str):
    async with aiosqlite.connect(settings.DATABASE_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO candidates (tg_id, fullname, email, role, language) VALUES (?, ?, ?, ?, ?)",
            (tg_id, fullname, email, role, language)
        )
        await db.commit()

async def log_turn(tg_id: int, role_prompt: str, user_answer: str):
    async with aiosqlite.connect(settings.DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO interview_logs (tg_id, role_prompt, user_answer) VALUES (?, ?, ?)",
            (tg_id, role_prompt, user_answer)
        )
        await db.commit()

async def get_transcript(tg_id: int) -> str:
    async with aiosqlite.connect(settings.DATABASE_PATH) as db:
        async with db.execute("SELECT role_prompt, user_answer FROM interview_logs WHERE tg_id = ? ORDER BY id ASC", (tg_id,)) as cursor:
            rows = await cursor.fetchall()
            transcript = ""
            for row in rows:
                transcript += f"Бот: {row[0]}\nКандидат: {row[1]}\n\n"
            return transcript

async def get_all_candidates():
    async with aiosqlite.connect(settings.DATABASE_PATH) as db:
        async with db.execute("SELECT tg_id, fullname, email, role, status FROM candidates") as cursor:
            return await cursor.fetchall()
