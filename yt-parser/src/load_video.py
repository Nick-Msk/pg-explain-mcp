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
import time

import psycopg
import yt_dlp
from psycopg.types.json import Jsonb
from youtube_transcript_api import YouTubeTranscriptApi

from src.db import connect, ensure_schema, get_db_dsn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("yt-parser")


# --- CLI ---------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Загружает метаданные и транскрипт YouTube-видео в PostgreSQL (таблица yt_source)."
    )
    p.add_argument("-l", "--host", default="localhost",
                   help="Хост/IP PostgreSQL (по умолчанию: localhost)")
    p.add_argument("-d", "--dbname", default="ytparse",
                   help="Имя базы данных (по умолчанию: ytparse)")
    p.add_argument("-u", "--user", default=os.getenv("PGUSER"),
                   help="Пользователь PostgreSQL (по умолчанию: из PGUSER или текущий OS-пользователь)")
    p.add_argument("-w", "--password", default=os.getenv("PGPASSWORD"),
                   help="Пароль PostgreSQL (по умолчанию: из PGPASSWORD)")
    p.add_argument("--port", type=int, default=int(os.getenv("PGPORT", "5432")),
                   help="Порт PostgreSQL (по умолчанию: 5432 или PGPORT)")
    p.add_argument("-y", "--url", default=None,
                   help="Ссылка на YouTube-видео (обязательно без -p)")
    p.add_argument("-p", "--process", action="store_true",
                   help="Обработать очередь: забрать все yt_source со status='pending'")
    p.add_argument("--watch", action="store_true",
                   help="С -p: не выходить, а ждать новые pending (поллинг)")
    p.add_argument("--poll-interval", type=int, default=10,
                   help="Интервал опроса в --watch, сек (по умолчанию: 10)")
    p.add_argument("--batch-size", type=int, default=10,
                   help="Сколько записей брать за один цикл (по умолчанию: 10)")
    p.add_argument("--recover", action="store_true",
                   help="С -p: вернуть 'processing' → 'pending' (зависшие >30 мин)")
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

    channel_yt_id = metadata.get("channel_id")
    channel_name = metadata.get("channel") or "Unknown"
    channel_db_id = None
    if channel_yt_id:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO yt_channel (channel_id, name)
                VALUES (%s, %s)
                ON CONFLICT (channel_id) DO UPDATE SET name = EXCLUDED.name
                RETURNING id
            """, (channel_yt_id, channel_name))
            channel_db_id = cur.fetchone()[0]
        conn.commit()

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
    # try to map channel
    # Привязываем видео к каналу
    if channel_db_id and source_id:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO yt_channel_vids (channel_id, source_id)
                VALUES (%s, %s) ON CONFLICT DO NOTHING
            """, (channel_db_id, source_id))
        conn.commit()

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


def recover_stuck(conn: psycopg.Connection, timeout_min: int = 30) -> int:
    """'processing' → 'pending' для записей, зависших дольше timeout_min."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE yt_source
            SET status='pending', updated_at=now()
            WHERE status='processing'
              AND updated_at < now() - make_interval(mins => %s)
            """,
            (timeout_min,),
        )
        n = cur.rowcount
    conn.commit()
    if n:
        logger.info(f"Восстановлено зависших 'processing': {n}")
    return n


def claim_pending(conn: psycopg.Connection, limit: int) -> list[tuple[int, str]]:
    """Атомарно забирает до limit 'pending' → 'processing' и возвращает их."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE yt_source
            SET status='processing', updated_at=now()
            WHERE id IN (
                SELECT id FROM yt_source
                WHERE status='pending'
                ORDER BY created_at
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, ytlink
            """,
            (limit,),
        )
        rows = cur.fetchall()
    conn.commit()
    return rows


def mark_failed(conn: psycopg.Connection, source_id: int, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE yt_source SET status='failed', error_message=%s, updated_at=now() WHERE id=%s",
            (error, source_id),
        )
    conn.commit()


def process_queue(conn: psycopg.Connection, args: argparse.Namespace) -> None:
    if args.recover:
        recover_stuck(conn)

    while True:
        batch = claim_pending(conn, args.batch_size)
        if not batch:
            if args.watch:
                logger.info(f"Очередь пуста, ждём {args.poll_interval}с...")
                time.sleep(args.poll_interval)
                continue
            logger.info("Очередь пуста, выходим.")
            break

        logger.info(f"Взято из очереди: {len(batch)}.")
        for source_id, ytlink in batch:
            logger.info(f"[id={source_id}] {ytlink}")
            try:
                process_video(conn, ytlink)   # upsert заменит status='processing' на 'ok'
            except Exception as e:
                logger.exception(f"[id={source_id}] упало: {e}")
                mark_failed(conn, source_id, str(e))

def main() -> int:
    args = parse_args()
    if not args.process and not args.url:
        logger.error("Укажите -y URL или -p (обработать очередь).")
        return 2

    dsn = get_db_dsn(
        host=args.host,
        dbname=args.dbname,
        user=args.user,
        password=args.password,
        port=args.port,
    )
    try:
        with connect(dsn) as conn:
            ensure_schema(conn)
            if args.process:
                process_queue(conn, args)
            else:
                process_video(conn, args.url)
    except psycopg.Error as e:
        logger.error(f"Ошибка PostgreSQL: {e}")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())

