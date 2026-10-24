#!/usr/bin/env python3
"""
Заполняет yt_chunks.embedding и (опционально) yt_source.embedding.

Пример:
    uv run embed_chunks.py -l localhost -d ytparse
    uv run embed_chunks.py -l localhost -d ytparse --limit 5      # для теста
    uv run embed_chunks.py -l localhost -d ytparse --force        # пересчитать всё
"""

import argparse
import logging
import os
import sys
import time

import psycopg
from sentence_transformers import SentenceTransformer

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
logger = logging.getLogger("embed_chunks")

DEFAULT_MODEL = "intfloat/multilingual-e5-large"
EXPECTED_DIM = 1024


# --- CLI ---------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Считает эмбеддинги для yt_chunks и yt_source."
    )
    p.add_argument("-l", "--host", default="localhost",
                   help="Хост PostgreSQL (по умолчанию: localhost)")
    p.add_argument("-d", "--dbname", default="ytparse",
                   help="Имя БД (по умолчанию: ytparse)")
    p.add_argument("-u", "--user", default=os.getenv("PGUSER"),
                   help="Пользователь PostgreSQL (по умолчанию: из PGUSER)")
    p.add_argument("-w", "--password", default=os.getenv("PGPASSWORD"),
                   help="Пароль PostgreSQL (по умолчанию: из PGPASSWORD)")
    p.add_argument("--port", type=int, default=int(os.getenv("PGPORT", "5432")),
                   help="Порт PostgreSQL (по умолчанию: 5432)")
    p.add_argument("-m", "--model", default=DEFAULT_MODEL,
                   help=f"Модель sentence-transformers (по умолчанию: {DEFAULT_MODEL})")
    p.add_argument("--device", default=None,
                   help="Устройство: cpu / mps / cuda (по умолчанию: авто)")
    p.add_argument("--batch-size", type=int, default=32,
                   help="Размер батча при энкодинге (по умолчанию: 32)")
    p.add_argument("--limit", type=int, default=None,
                   help="Обработать не более N чанков (для теста)")
    p.add_argument("--force", action="store_true",
                   help="Пересчитать даже те чанки, у которых embedding уже есть")
    p.add_argument("--skip-source", action="store_true",
                   help="Не обновлять yt_source.embedding (только чанки)")
    return p.parse_args()


# --- Подключение и модель ----------------------------------------------------

def build_model(name: str, device: str | None) -> SentenceTransformer:
    logger.info(f"Загрузка модели: {name} (device={device or 'auto'})")
    model = SentenceTransformer(name, device=device)
    dim = model.get_embedding_dimension()
    if dim != EXPECTED_DIM:
        raise RuntimeError(
            f"Модель '{name}' даёт размерность {dim}, а схема ждёт {EXPECTED_DIM}. "
            f"Либо возьмите модель с {EXPECTED_DIM} измерениями, либо сделайте "
            f"ALTER TABLE ... TYPE vector({dim})."
        )
    logger.info(f"Модель загружена, размерность = {dim}.")
    return model


def passage_prefix(model_name: str) -> str:
    """e5 требует префикс 'passage: ' для документов, bge — без префикса."""
    if "e5" in model_name.lower():
        return "passage: "
    return ""


# --- Эмбеддинг чанков --------------------------------------------------------

def fetch_chunks(conn: psycopg.Connection, force: bool, limit: int | None) -> list[tuple[int, str]]:
    sql = "SELECT id, content FROM yt_chunks"
    if not force:
        sql += " WHERE embedding IS NULL"
    sql += " ORDER BY source_id, chunk_no"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def save_chunk_embeddings(conn: psycopg.Connection, rows: list[tuple[int, list[float]]]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE yt_chunks SET embedding = %s WHERE id = %s",
            [(vec, chunk_id) for chunk_id, vec in rows],
        )
    conn.commit()


def embed_chunks(
    model: SentenceTransformer,
    conn: psycopg.Connection,
    args: argparse.Namespace,
) -> int:
    rows = fetch_chunks(conn, force=args.force, limit=args.limit)
    if not rows:
        logger.info("Нет чанков для обработки (все эмбеддинги уже есть).")
        return 0

    logger.info(f"Чанков к обработке: {len(rows)}.")
    prefix = passage_prefix(args.model)
    ids = [r[0] for r in rows]
    texts = [f"{prefix}{r[1]}" for r in rows]

    vectors = model.encode(
        texts,
        batch_size=args.batch_size,
        normalize_embeddings=True,   # e5/bge рекомендуют нормализацию
        show_progress_bar=True,
        convert_to_numpy=True,
    )

    save_chunk_embeddings(conn, list(zip(ids, vectors.tolist(), strict=True)))
    logger.info(f"Записано эмбеддингов в yt_chunks: {len(rows)}.")
    return len(rows)

def embed_one_source(conn, model, sid: int, args) -> None:
    """Считает эмбеддинги для всех чанков одного видео, у которых их еще нет."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, content FROM yt_chunks
            WHERE source_id = %s AND embedding IS NULL
            ORDER BY chunk_no
        """, (sid,))
        rows = cur.fetchall()

    if not rows:
        return

    prefix = passage_prefix(args.model)
    ids = [r[0] for r in rows]
    texts = [f"{prefix}{r[1]}" for r in rows]

    vectors = model.encode(
        texts,
        batch_size=args.batch_size,
        normalize_embeddings=True,
        show_progress_bar=False,
        convert_to_numpy=True,
    )

    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE yt_chunks SET embedding = %s WHERE id = %s",
            [(vec.tolist(), chunk_id) for chunk_id, vec in zip(ids, vectors, strict=True)]
        )
    conn.commit()


def process_embed_queue(conn, args, model) -> None:
    recover_stuck(conn)

    while True:
        ids = claim_by_status(conn, 'loaded', 'process_embed', args.batch_size, actor='cli')
        if not ids:
            if args.watch:
                time.sleep(args.poll_interval)
                recover_stuck(conn)
                continue
            break

        for sid in ids:
            try:
                embed_one_source(conn, model, sid, args)
                if source_fully_embedded(conn, sid):
                    update_source_embedding(conn, sid)
                    set_status(conn, sid, 'embedded', actor='cli')
                else:
                    # какой-то чанк упал — оставляем в 'loaded', чтобы при следующем прогоне доделать
                    set_status(conn, sid, 'loaded', comment='partial chunks embedded')
            except Exception as e:
                logger.exception(f"[source_id={sid}] embed crashed: {e}")
                set_status(conn, sid, 'failed_embedded', comment=str(e)[:500], actor='cli')

def source_fully_embedded(conn, sid) -> bool:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
              EXISTS(SELECT 1 FROM yt_chunks WHERE source_id=%s)
              AND NOT EXISTS(SELECT 1 FROM yt_chunks WHERE source_id=%s AND embedding IS NULL)
        """, (sid, sid))
        return cur.fetchone()[0]

def update_source_embedding(conn, sid) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE yt_source SET embedding = sub.avg_emb, updated_at = now()
            FROM (SELECT AVG(embedding)::vector(1024) AS avg_emb FROM yt_chunks WHERE source_id=%s) sub
            WHERE id = %s
        """, (sid, sid))
    conn.commit()


# --- Эмбеддинг документа (усреднение чанков) ---------------------------------

def recompute_source_embeddings(conn: psycopg.Connection, force: bool) -> int:
    """
    yt_source.embedding = усреднение эмбеддингов его чанков.
    Если хотя бы один чанк без эмбеддинга — источник пропускается (лучше не
    записывать заведомо неполный «средний» вектор).
    """
    sql = """
        WITH ready AS (
            SELECT source_id, AVG(embedding)::vector(1024) AS avg_emb
            FROM yt_chunks
            GROUP BY source_id
            HAVING COUNT(*) FILTER (WHERE embedding IS NULL) = 0
               AND COUNT(*) > 0
        )
        UPDATE yt_source s
        SET embedding = r.avg_emb, updated_at = now()
        FROM ready r
        WHERE s.id = r.source_id
    """
    if not force:
        sql += " AND s.embedding IS NULL"

    with conn.cursor() as cur:
        cur.execute(sql)
        updated = cur.rowcount
    conn.commit()
    logger.info(f"Обновлено yt_source.embedding: {updated}.")
    return updated

# --- Legacy режим (глобальный поиск чанков) ----------------------------------

def process_legacy(conn: psycopg.Connection, args: argparse.Namespace, model) -> None:
    sql = "SELECT id, content FROM yt_chunks"
    if not args.force:
        sql += " WHERE embedding IS NULL"
    sql += " ORDER BY source_id, chunk_no"
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"

    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()

    if not rows:
        logger.info("Нет чанков для обработки.")
        return

    logger.info(f"Чанков к обработке: {len(rows)}.")
    prefix = passage_prefix(args.model)
    ids = [r[0] for r in rows]
    texts = [f"{prefix}{r[1]}" for r in rows]

    vectors = model.encode(
        texts,
        batch_size=args.batch_size,
        normalize_embeddings=True,
        show_progress_bar=True,
        convert_to_numpy=True,
    )

    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE yt_chunks SET embedding = %s WHERE id = %s",
            [(vec.tolist(), chunk_id) for chunk_id, vec in zip(ids, vectors, strict=True)]
        )
    conn.commit()
    logger.info(f"Записано эмбеддингов: {len(rows)}.")

    # Обновляем средние для тех источников, где всё посчитано
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE yt_source s SET embedding = sub.avg_emb, updated_at = now()
            FROM (
                SELECT source_id, AVG(embedding)::vector(1024) AS avg_emb
                FROM yt_chunks
                GROUP BY source_id
                HAVING COUNT(*) FILTER (WHERE embedding IS NULL) = 0
            ) sub
            WHERE s.id = sub.source_id
        """)
    conn.commit()

# --- main --------------------------------------------------------------------

def main() -> int:
    args = parse_args()

    try:
        model = build_model(args.model, args.device)
    except Exception as e:
        logger.error(f"Не удалось загрузить модель: {e}")
        return 1

    dsn = get_db_dsn(args.host, args.dbname, args.user, args.password, args.port)
    try:
        with connect(dsn) as conn:
            ensure_schema(conn)
            if args.process:
                process_embed_queue(conn, args, model)
            else:
                process_legacy(conn, args, model)
    except psycopg.Error as e:
        logger.error(f"Ошибка PostgreSQL: {e}")
        return 1

    return 0

if __name__ == "__main__":
    sys.exit(main())

