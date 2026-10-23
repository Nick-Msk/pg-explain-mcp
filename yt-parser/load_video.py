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


SCHEMA_SQL = """
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
"""


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
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()
    logger.info("Схема проверена (CREATE TABLE IF NOT EXISTS).")


def save_to_db(
    conn: psycopg.Connection,
    ytlink: str,
    metadata: dict,
    contain: str | None,
    status: str = "ok",
    error: str | None = None,
) -> None:
    sql = """
        INSERT INTO yt_source (ytlink, metadata, contain, status, error_message, updated_at)
        VALUES (%s, %s, %s, %s, %s, now())
        ON CONFLICT (ytlink) DO UPDATE SET
            metadata      = EXCLUDED.metadata,
            contain       = EXCLUDED.contain,
            status        = EXCLUDED.status,
            error_message = EXCLUDED.error_message,
            updated_at    = now();
    """
    with conn.cursor() as cur:
        cur.execute(sql, (ytlink, Jsonb(metadata), contain, status, error))
    conn.commit()
    logger.info(f"Запись для {ytlink} сохранена (status='{status}').")


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

    try:
        transcript = fetch_transcript(video_id)
        logger.info(f"Транскрипт получен ({len(transcript)} символов).")
    except Exception as e:
        logger.error(f"Ошибка транскрипта: {e}")
        save_to_db(conn, url, metadata, None, status="failed", error=str(e))
        return

    save_to_db(conn, url, metadata, transcript, status="ok")


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

