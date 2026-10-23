#!/usr/bin/env python3
"""
Загрузка метаданных и транскрипта YouTube-видео в PostgreSQL.

Пример:
    uv run load_video.py -l localhost -d ytparse -y www.youtube.com/watch?v=NTAVWjtSuc4
"""

import argparse
import logging
import os
import re
import sys

import psycopg
import yt_dlp
from psycopg.types.json import Jsonb
from youtube_transcript_api import YouTubeTranscriptApi

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("yt-parser")

SCHEMA_STATEMENTS = [
    # --- 1. Расширения (должны идти первыми) ---
    "CREATE EXTENSION IF NOT EXISTS vector",
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",

    # --- 2. Базовые таблицы ---
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
    """
    CREATE TABLE IF NOT EXISTS yt_tags (
        tag         TEXT PRIMARY KEY,
        description TEXT,
        weight      NUMERIC NOT NULL DEFAULT 1.0,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,

    # --- 3. Колонки поиска на yt_source ---
    "ALTER TABLE yt_source ADD COLUMN IF NOT EXISTS embedding vector(1024)",
    "ALTER TABLE yt_source ADD COLUMN IF NOT EXISTS contain_tsv tsvector "
    "GENERATED ALWAYS AS (to_tsvector('russian', coalesce(contain, ''))) STORED",

    # --- 4. Индексы на yt_source ---
    "CREATE INDEX IF NOT EXISTS yt_source_embedding_hnsw_idx "
    "ON yt_source USING hnsw (embedding vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS yt_source_contain_tsv_idx "
    "ON yt_source USING gin (contain_tsv)",
    "CREATE INDEX IF NOT EXISTS yt_source_contain_trgm_idx "
    "ON yt_source USING gin (contain gin_trgm_ops)",

    # --- 5. Таблица чанков ---
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

    # --- 6. Индексы на yt_chunks ---
    "CREATE INDEX IF NOT EXISTS yt_chunks_tsv_idx "
    "ON yt_chunks USING gin (content_tsv)",
    "CREATE INDEX IF NOT EXISTS yt_chunks_embedding_hnsw_idx "
    "ON yt_chunks USING hnsw (embedding vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS yt_chunks_content_trgm_idx "
    "ON yt_chunks USING gin (content gin_trgm_ops)",
]

# --- CLI ---------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Загружает метаданные и транскрипт YouTube-видео в PostgreSQL (таблица yt_source)."
    )
    p.add_argument("-l", "--host", default="localhost",
                   help="Хост/IP PostgreSQL (по умолчанию: localhost)")
    p.add_argument("-d", "--dbname", default="ytparse",
                   help="Имя базы данных (по умолчанию: ytparse)")
    p.add_argument("-y", "--url", required=True,
                   help="Ссылка на YouTube-видео (можно без https://)")
    p.add_argument("-u", "--user", default=os.getenv("PGUSER"),
                   help="Пользователь PostgreSQL (по умолчанию: из PGUSER или текущий OS-пользователь)")
    p.add_argument("-w", "--password", default=os.getenv("PGPASSWORD"),
                   help="Пароль PostgreSQL (по умолчанию: из PGPASSWORD)")
    p.add_argument("--port", type=int, default=int(os.getenv("PGPORT", "5432")),
                   help="Порт PostgreSQL (по умолчанию: 5432 или PGPORT)")
    return p.parse_args()


# --- Вспомогательные функции -------------------------------------------------

def normalize_url(url: str) -> str:
    """Дополняет URL схемой, если её нет."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")
    return url


def extract_video_id(url: str) -> str:
    """Достаёт 11-символьный video ID из URL."""
    patterns = [
        r"(?:v=|\/)([0-9A-Za-z_-]{11})(?:[&?]|$)",
        r"youtu\.be\/([0-9A-Za-z_-]{11})",
        r"shorts\/([0-9A-Za-z_-]{11})",
    ]
    for pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    raise ValueError(f"Не удалось извлечь video ID из URL: {url}")


def fetch_metadata(url: str) -> dict:
    """Извлекает метаданные видео через yt-dlp."""
    ydl_opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info)


def fetch_transcript(video_id: str, languages: list[str] | None = None) -> str:
    """Возвращает сплошной текст транскрипта (по умолчанию ru → en)."""
    languages = languages or ["ru", "en"]
    ytt_api = YouTubeTranscriptApi()
    fetched = ytt_api.fetch(video_id, languages=languages)
    return " ".join(snippet.text for snippet in fetched)


# --- Работа с БД -------------------------------------------------------------

def connect(args: argparse.Namespace) -> psycopg.Connection:
    kwargs = {
        "host": args.host,
        "port": args.port,
        "dbname": args.dbname,
    }
    if args.user:
        kwargs["user"] = args.user
    if args.password:
        kwargs["password"] = args.password
    return psycopg.connect(**kwargs)

def ensure_schema(conn: psycopg.Connection) -> None:
    for stmt in SCHEMA_STATEMENTS:
        label = stmt.strip().split("\n", 1)[0][:60]
        try:
            with conn.cursor() as cur:
                cur.execute(stmt)
            conn.commit()
        except psycopg.Error as e:
            conn.rollback()
            logger.warning(f"[skip] {label}…: {e}")
    logger.info("Схема проверена.")

def save_to_db(
    conn: psycopg.Connection,
    ytlink: str,
    metadata: dict,
    contain: str | None,
    status: str = "ok",
    error: str | None = None,
) -> int:
    sql = """
        INSERT INTO yt_source (ytlink, metadata, contain, status, error_message, updated_at)
        VALUES (%s, %s, %s, %s, %s, now())
        ON CONFLICT (ytlink) DO UPDATE SET
            metadata      = EXCLUDED.metadata,
            contain       = EXCLUDED.contain,
            status        = EXCLUDED.status,
            error_message = EXCLUDED.error_message,
            updated_at    = now()
        RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (ytlink, Jsonb(metadata), contain, status, error))
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return row[0]

# --- Основной пайплайн -------------------------------------------------------

def process_video(conn: psycopg.Connection, raw_url: str) -> None:
    url = normalize_url(raw_url)
    video_id = extract_video_id(url)
    logger.info(f"Обработка: {url} (video_id={video_id})")

    metadata: dict = {}
    try:
        metadata = fetch_metadata(url)
        logger.info("Метаданные извлечены.")
    except Exception as e:
        logger.error(f"Ошибка метаданных: {e}")
        save_to_db(conn, url, metadata, None, status="failed", error=str(e))
        return

    # Теги — отдельно и не фатально
    try:
        tags = extract_tags(metadata)
        inserted = save_tags(conn, tags)
        logger.info(f"Тегов извлечено: {len(tags)}, новых в БД: {inserted}.")
    except Exception as e:
        logger.warning(f"Не удалось сохранить теги: {e}")

    try:
        transcript = fetch_transcript(video_id)
        logger.info(f"Транскрипт получен ({len(transcript)} символов).")
    except Exception as e:
        logger.error(f"Ошибка транскрипта: {e}")
        save_to_db(conn, url, metadata, None, status="failed", error=str(e))
        return

    source_id = save_to_db(conn, url, metadata, transcript, status="ok")
    try:
        n = save_chunks(conn, source_id, transcript, max_bytes=4096)
        logger.info(f"Чанков сохранено: {n}.")
    except psycopg.Error as e:
        logger.warning(f"Не удалось сохранить чанки: {e}")

def extract_tags(metadata: dict) -> list[str]:
    """Возвращает список тегов из метаданных yt-dlp (без пустых и не-строк)."""
    raw = metadata.get("tags") or []
    return [t.strip() for t in raw if isinstance(t, str) and t.strip()]


def save_tags(conn: psycopg.Connection, tags: list[str]) -> int:
    """Вставляет теги в yt_tags. Возвращает число фактически новых записей."""
    if not tags:
        return 0
    sql = """
        INSERT INTO yt_tags (tag, description, weight)
        VALUES (%s, NULL, 1.0)
        ON CONFLICT (tag) DO NOTHING;
    """
    with conn.cursor() as cur:
        cur.executemany(sql, [(t,) for t in tags])
        inserted = cur.rowcount  # с DO NOTHING считает только реально вставленные
    conn.commit()
    return inserted

def chunk_text(text: str, max_bytes: int = 4096) -> list[str]:
    """
    Режет текст на куски ≤ max_bytes (UTF-8), не разрывая слов.
    Слова разделяются пробелом; если слово в хвосте не влезает — целиком уходит
    в следующий чанк. Слово длиннее max_bytes (не встречается в норме)
    становится отдельным чанком с warning.
    """
    if not text:
        return []

    words = text.split()
    chunks: list[str] = []
    buf: list[str] = []
    buf_bytes = 0

    for w in words:
        w_bytes = len(w.encode("utf-8"))
        if w_bytes > max_bytes:
            logger.warning(
                "Слово длиннее %d байт (%d) — режем принудительно: %.40s…",
                max_bytes, w_bytes, w,
            )
            if buf:
                chunks.append(" ".join(buf))
                buf, buf_bytes = [], 0
            chunks.append(w)
            continue

        sep = 0 if not buf else 1  # один пробел-разделитель
        if buf and buf_bytes + sep + w_bytes > max_bytes:
            chunks.append(" ".join(buf))
            buf = [w]
            buf_bytes = w_bytes
        else:
            buf.append(w)
            buf_bytes += sep + w_bytes

    if buf:
        chunks.append(" ".join(buf))
    return chunks

def save_chunks(
    conn: psycopg.Connection,
    source_id: int,
    contain: str,
    max_bytes: int = 4096,
) -> int:
    chunks = chunk_text(contain, max_bytes=max_bytes)
    if not chunks:
        return 0
    with conn.cursor() as cur:
        cur.execute("DELETE FROM yt_chunks WHERE source_id = %s", (source_id,))
        cur.executemany(
            "INSERT INTO yt_chunks (source_id, chunk_no, content) VALUES (%s, %s, %s)",
            [(source_id, i, c) for i, c in enumerate(chunks)],
        )
    conn.commit()
    return len(chunks)

def main() -> int:
    args = parse_args()
    try:
        with connect(args) as conn:
            ensure_schema(conn)
            process_video(conn, args.url)
    except psycopg.Error as e:
        logger.error(f"Ошибка PostgreSQL: {e}")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())

