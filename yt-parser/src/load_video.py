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

# Импортируем всё нужное из нашего модуля БД
from src.db import (
    claim_by_status,
    connect,
    ensure_schema,
    get_db_dsn,
    recover_stuck,
    set_status,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("yt-parser")

# --- Вспомогательные функции -------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Загружает YouTube-видео в PostgreSQL.")
    p.add_argument("-l", "--host", default="localhost")
    p.add_argument("-d", "--dbname", default="ytparse")
    p.add_argument("-u", "--user", default=os.getenv("PGUSER"))
    p.add_argument("-w", "--password", default=os.getenv("PGPASSWORD"))
    p.add_argument("--port", type=int, default=int(os.getenv("PGPORT", "5432")))

    p.add_argument("-y", "--url", default=None, help="Ссылка на YouTube-видео")
    p.add_argument("-p", "--process", action="store_true", help="Обработать очередь")
    p.add_argument("--watch", action="store_true", help="Ждать новые видео")
    p.add_argument("--poll-interval", type=int, default=10, help="Интервал опроса, сек")
    p.add_argument("--batch-size", type=int, default=10, help="Сколько брать за раз")
    return p.parse_args()

def normalize_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")
    return url

def extract_video_id(url: str) -> str:
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
    ydl_opts = {"skip_download": True, "quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info)

def fetch_transcript(video_id: str, languages: list[str] | None = None) -> str:
    languages = languages or ["ru", "en"]
    ytt_api = YouTubeTranscriptApi()
    fetched = ytt_api.fetch(video_id, languages=languages)
    return " ".join(snippet.text for snippet in fetched)

def chunk_text(text: str, max_bytes: int = 4096) -> list[str]:
    if not text:
        return []
    words = text.split()
    chunks, buf, buf_bytes = [], [], 0
    for w in words:
        w_bytes = len(w.encode("utf-8"))
        if w_bytes > max_bytes:
            if buf:
                chunks.append(" ".join(buf))
                buf, buf_bytes = [], 0
            chunks.append(w)
            continue
        sep = 0 if not buf else 1
        if buf and buf_bytes + sep + w_bytes > max_bytes:
            chunks.append(" ".join(buf))
            buf, buf_bytes = [w], w_bytes
        else:
            buf.append(w)
            buf_bytes += sep + w_bytes
    if buf:
        chunks.append(" ".join(buf))
    return chunks

# --- Работа с БД -------------------------------------------------------------

def upsert_source(conn: psycopg.Connection, ytlink: str, metadata: dict, contain: str | None) -> int:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO yt_source (ytlink, metadata, contain, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (ytlink) DO UPDATE SET
                metadata   = EXCLUDED.metadata,
                contain    = EXCLUDED.contain,
                updated_at = now()
            RETURNING id
        """, (ytlink, Jsonb(metadata), contain))
        source_id = cur.fetchone()[0]
        # УДАЛИТЕ строку: INSERT INTO yt_status ...
    conn.commit()
    return source_id

def save_channel(conn: psycopg.Connection, source_id: int, metadata: dict) -> None:
    channel_yt_id = metadata.get("channel_id")
    if not channel_yt_id:
        return
    channel_name = metadata.get("channel") or "Unknown"
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO yt_channel (channel_id, name)
            VALUES (%s, %s)
            ON CONFLICT (channel_id) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
        """, (channel_yt_id, channel_name))
        channel_db_id = cur.fetchone()[0]

        cur.execute("""
            INSERT INTO yt_channel_vids (channel_id, source_id)
            VALUES (%s, %s) ON CONFLICT DO NOTHING
        """, (channel_db_id, source_id))
    conn.commit()

def extract_tags(metadata: dict) -> list[str]:
    raw = metadata.get("tags") or []
    return [t.strip() for t in raw if isinstance(t, str) and t.strip()]

def save_tags(conn: psycopg.Connection, tags: list[str]) -> None:
    if not tags:
        return
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO yt_tags (tag, description, weight) VALUES (%s, NULL, 1.0) ON CONFLICT (tag) DO NOTHING",
            [(t,) for t in tags]
        )
    conn.commit()

def link_source_tags(conn: psycopg.Connection, source_id: int, tags: list[str]) -> None:
    if not tags:
        return
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO yt_source_tags (source_id, tag, weight) VALUES (%s, %s, 1.0) ON CONFLICT DO NOTHING",
            [(source_id, t) for t in tags]
        )
    conn.commit()

def save_chunks(conn: psycopg.Connection, source_id: int, contain: str, max_bytes: int = 4096) -> int:
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

# --- Пайплайн ----------------------------------------------------------------

def process_video(conn: psycopg.Connection, source_id: int, url: str) -> None:
    video_id = extract_video_id(url)
    logger.info(f"[source_id={source_id}] Загрузка: {url}")

    try:
        metadata = fetch_metadata(url)
        tags = extract_tags(metadata)
        transcript = fetch_transcript(video_id)
    except Exception as e:
        logger.error(f"[source_id={source_id}] load failed: {e}")
        set_status(conn, source_id, 'failed_load', comment=str(e)[:500])
        return

    sid = upsert_source(conn, url, metadata, transcript)
    save_channel(conn, sid, metadata)
    save_tags(conn, tags)
    link_source_tags(conn, sid, tags)

    n_chunks = save_chunks(conn, sid, transcript)
    set_status(conn, source_id, 'loaded', actor='cli')
    logger.info(f"[source_id={source_id}] loaded ({n_chunks} chunks, {len(tags)} tags)")

def process_queue(conn: psycopg.Connection, args: argparse.Namespace) -> None:
    recover_stuck(conn)   # Сразу лечим зависших

    while True:
        ids = claim_by_status(conn, 'added', 'process_load', args.batch_size, actor='cli')
        if not ids:
            if args.watch:
                logger.info(f"Очередь пуста, ждём {args.poll_interval}с...")
                time.sleep(args.poll_interval)
                recover_stuck(conn)
                continue
            break

        logger.info(f"Взято из очереди: {len(ids)}.")
        for sid in ids:
            with conn.cursor() as cur:
                cur.execute("SELECT ytlink FROM yt_source WHERE id = %s", (sid,))
                url = cur.fetchone()[0]
            try:
                process_video(conn, sid, url)
            except Exception as e:
                logger.exception(f"[source_id={sid}] crashed: {e}")
                set_status(conn, sid, 'failed_load', comment=str(e)[:500], actor='cli')

def main() -> int:
    args = parse_args()
    if not args.process and not args.url:
        logger.error("Укажите -y URL или -p (обработать очередь).")
        return 2

    dsn = get_db_dsn(args.host, args.dbname, args.user, args.password, args.port)
    try:
        with connect(dsn) as conn:
            ensure_schema(conn)
            if args.process:
                process_queue(conn, args)
            else:
                url = normalize_url(args.url)
                # Триггер сам создаст yt_status
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO yt_source (ytlink) VALUES (%s) 
                        ON CONFLICT (ytlink) DO UPDATE SET ytlink=EXCLUDED.ytlink 
                        RETURNING id
                    """, (url,))
                    sid = cur.fetchone()[0]
                conn.commit()
                process_video(conn, sid, url)
    except psycopg.Error as e:
        logger.error(f"Ошибка PostgreSQL: {e}")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())

