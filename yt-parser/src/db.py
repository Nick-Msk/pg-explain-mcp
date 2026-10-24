import logging
import os

import psycopg

logger = logging.getLogger(__name__)

def get_db_dsn(host: str = "localhost", dbname: str = "ytparse", user: str = None, password: str = None, port: int = 5432) -> str:
    if user or password:
        user = user or os.getenv("PGUSER", "")
        password = password or os.getenv("PGPASSWORD", "")
        return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"
    return f"postgresql://{host}:{port}/{dbname}"

def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn)

# --- Вся схема БД собрана здесь ---

SCHEMA_STATEMENTS = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",

    # 1. Источники (видео)
    """
    CREATE TABLE IF NOT EXISTS yt_source (
        id            SERIAL PRIMARY KEY,
        ytlink        TEXT NOT NULL UNIQUE,
        metadata      JSONB,
        contain       TEXT,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,

    # 2. Теги
    """
    CREATE TABLE IF NOT EXISTS yt_tags (
        tag         TEXT PRIMARY KEY,
        description TEXT,
        weight      NUMERIC NOT NULL DEFAULT 1.0,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,

    # 3. Индексы и колонки для yt_source
    "ALTER TABLE yt_source ADD COLUMN IF NOT EXISTS embedding vector(1024)",
    "ALTER TABLE yt_source ADD COLUMN IF NOT EXISTS contain_tsv tsvector GENERATED ALWAYS AS (to_tsvector('russian', coalesce(contain, ''))) STORED",
    "CREATE INDEX IF NOT EXISTS yt_source_embedding_hnsw_idx ON yt_source USING hnsw (embedding vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS yt_source_contain_tsv_idx ON yt_source USING gin (contain_tsv)",
    "CREATE INDEX IF NOT EXISTS yt_source_contain_trgm_idx ON yt_source USING gin (contain gin_trgm_ops)",

    # 4. Чанки
    """
    CREATE TABLE IF NOT EXISTS yt_chunks (
        id          BIGSERIAL PRIMARY KEY,
        source_id   INT  NOT NULL REFERENCES yt_source(id) ON DELETE CASCADE,
        chunk_no    INT  NOT NULL,
        content     TEXT NOT NULL,
        content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('russian', content)) STORED,
        embedding   vector(1024),
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (source_id, chunk_no)
    );
    """,
    "CREATE INDEX IF NOT EXISTS yt_chunks_tsv_idx ON yt_chunks USING gin (content_tsv)",
    "CREATE INDEX IF NOT EXISTS yt_chunks_embedding_hnsw_idx ON yt_chunks USING hnsw (embedding vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS yt_chunks_content_trgm_idx ON yt_chunks USING gin (content gin_trgm_ops)",

    # 5. Каналы и связь
    """
    CREATE TABLE IF NOT EXISTS yt_channel (
        id SERIAL PRIMARY KEY,
        channel_id TEXT UNIQUE NOT NULL,
        name TEXT,
        created_at TIMESTAMPTZ DEFAULT now()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS yt_channel_vids (
        channel_id INT REFERENCES yt_channel(id) ON DELETE CASCADE,
        source_id INT REFERENCES yt_source(id) ON DELETE CASCADE,
        PRIMARY KEY (channel_id, source_id)
    );
    """,

    # 6. Результаты LLM
    """
    CREATE TABLE IF NOT EXISTS yt_solution (
        id SERIAL PRIMARY KEY,
        channel_id INT REFERENCES yt_channel(id) ON DELETE SET NULL,
        source_id INT REFERENCES yt_source(id) ON DELETE CASCADE UNIQUE,
        raw_text TEXT,
        age INT,
        region TEXT,
        salary TEXT,
        status TEXT CHECK (status IN ('working', 'resigned', 'fired', 'in_thinking', 'unknown')),
        reasons JSONB,
        created_at TIMESTAMPTZ DEFAULT now()
    );
    """,
    # 7. State machine statuses
    """
    CREATE TABLE IF NOT EXISTS yt_status (
        source_id   INT PRIMARY KEY REFERENCES yt_source(id) ON DELETE CASCADE,
        status      TEXT NOT NULL DEFAULT 'added'
                    CHECK (status IN (
                        'added', 'process_load', 'loaded', 'failed_load',
                        'process_embed', 'embedded', 'failed_embedded',
                        'process_solution', 'solution', 'failed_solution'
                    )),
        comment     TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    # Автоматическое создание yt_status при вставке в yt_source
    """
    CREATE OR REPLACE FUNCTION auto_create_yt_status()
    RETURNS TRIGGER AS $$
    BEGIN
        INSERT INTO yt_status (source_id, status) VALUES (NEW.id, 'added');
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """,
    """
    DROP TRIGGER IF EXISTS trg_auto_create_yt_status ON yt_source;
    CREATE TRIGGER trg_auto_create_yt_status
    AFTER INSERT ON yt_source
    FOR EACH ROW EXECUTE FUNCTION auto_create_yt_status();
    """,
    # Бэкфилл для старых записей (у которых триггера еще не было)
    """
    INSERT INTO yt_status (source_id, status)
    SELECT id, 'added' FROM yt_source
    ON CONFLICT (source_id) DO NOTHING;
    """
]

def ensure_schema(conn: psycopg.Connection) -> None:
    """Создаёт всю схему. Каждый statement отдельно, чтобы отсутствие pgvector не роняло базовые таблицы."""
    for stmt in SCHEMA_STATEMENTS:
        label = stmt.strip().split("\n", 1)[0][:60]
        try:
            with conn.cursor() as cur:
                cur.execute(stmt)
            conn.commit()
        except psycopg.Error as e:
            conn.rollback()
            logger.warning(f"[skip] {label}…: {e}")
    logger.info("Схема БД проверена/создана.")

def claim_by_status(conn: psycopg.Connection, from_status: str, to_status: str, limit: int, actor: str = 'default') -> list[int]:
    with conn.cursor() as cur:
        # Устанавливаем actor для триггера
        cur.execute("SELECT set_config('app.actor', %s, true)", (actor,))
        cur.execute("""
            UPDATE yt_status
            SET status = %s, updated_at = now()
            WHERE source_id IN (
                SELECT source_id FROM yt_status
                WHERE status = %s
                ORDER BY updated_at
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            RETURNING source_id
        """, (to_status, from_status, limit))
        rows = cur.fetchall()
    conn.commit()
    return [r[0] for r in rows]

def set_status(conn: psycopg.Connection, source_id: int, status: str, comment: str | None = None, actor: str = 'default') -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.actor', %s, true)", (actor,))
        cur.execute("""
            UPDATE yt_status
            SET status = %s, comment = %s, updated_at = now()
            WHERE source_id = %s
        """, (status, comment, source_id))
    conn.commit()

def recover_stuck(conn: psycopg.Connection, timeout_min: int = 30) -> dict[str, int]:
    """Откатывает 'process_*', зависшие дольше timeout_min, в предыдущий статус."""
    rollback = {
        'process_load':     'added',
        'process_embed':    'loaded',
        'process_solution': 'embedded',
    }
    result = {}
    with conn.cursor() as cur:
        for from_s, to_s in rollback.items():
            cur.execute("""
                UPDATE yt_status
                SET status = %s, updated_at = now(),
                    comment = 'recovered (timeout)'
                WHERE status = %s
                  AND updated_at < now() - make_interval(mins => %s)
            """, (to_s, from_s, timeout_min))
            n = cur.rowcount
            if n:
                result[from_s] = n
    conn.commit()
    return result

