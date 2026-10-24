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
        status        TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'processing', 'ok', 'failed')),
        error_message TEXT,
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

