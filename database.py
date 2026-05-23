import aiosqlite

DB_PATH = "auditcore.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER UNIQUE,
                language TEXT DEFAULT 'ru',
                name TEXT,
                email TEXT,
                role TEXT,
                turn_count INTEGER DEFAULT 0,
                suspicious_count INTEGER DEFAULT 0,
                timeout_count INTEGER DEFAULT 0,
                transcript TEXT DEFAULT '',
                final_verdict TEXT DEFAULT '',
                status TEXT DEFAULT 'in_progress',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.commit()

async def insert_candidate(tg_id: int, language: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO candidates (tg_id, language) VALUES (?, ?)",
            (tg_id, language)
        )
        await db.commit()

async def upsert_candidate(tg_id: int, **kwargs):
    if not kwargs:
        return
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [tg_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE candidates SET {fields} WHERE tg_id = ?", values
        )
        await db.commit()

async def get_candidate(tg_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM candidates WHERE tg_id = ?", (tg_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

async def get_all_candidates() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM candidates") as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

async def append_transcript(tg_id: int, text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE candidates SET transcript = transcript || ? WHERE tg_id = ?",
            (text + "\n", tg_id)
        )
        await db.commit()
