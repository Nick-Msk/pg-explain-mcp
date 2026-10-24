#!/usr/bin/env python3
"""
Анализ видео через LM Studio и заполнение yt_solution.
Пример: uv run src/exec_llm.py -d ytparse -p
"""

import argparse
import json
import logging
import os
import sys
import time

import psycopg
from openai import OpenAI
from psycopg.types.json import Jsonb

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
logger = logging.getLogger("exec_llm")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Заполняет yt_solution через LM Studio.")
    p.add_argument("-l", "--host", default="localhost")
    p.add_argument("-d", "--dbname", default="ytparse")
    p.add_argument("-u", "--user", default=os.getenv("PGUSER"))
    p.add_argument("-w", "--password", default=os.getenv("PGPASSWORD"))
    p.add_argument("--port", type=int, default=int(os.getenv("PGPORT", "5432")))

    p.add_argument("-p", "--process", action="store_true", help="Обработать очередь (embedded -> solution)")
    p.add_argument("--watch", action="store_true", help="Ждать новые embedded")
    p.add_argument("--poll-interval", type=int, default=10)
    p.add_argument("--batch-size", type=int, default=10)

    p.add_argument("--lm-url", default="http://127.0.0.1:1234/v1", help="URL LM Studio")
    p.add_argument("--lm-model", default="local-model", help="Имя модели (LM Studio игнорирует)")
    return p.parse_args()


def analyze_text(client: OpenAI, text: str, model: str) -> tuple[dict | None, str | None]:
    # Передаем схему, чтобы LM Studio был счастлив и модель не ломала JSON
    schema = {
        "type": "object",
        "properties": {
            "age": {"type": ["integer", "null"]},
            "region": {"type": ["string", "null"]},
            "salary": {"type": ["string", "null"]},
            "status": {"type": "string", "enum": ["working", "resigned", "fired", "in_thinking", "unknown"]},
            "reasons": {"type": "array", "items": {"type": "string"}},
            "raw_text": {"type": "string"}
        },
        "required": ["age", "region", "salary", "status", "reasons", "raw_text"],
        "additionalProperties": False
    }

    prompt = f"""Проанализируй текст интервью учителя.
Если данных нет — ставь null. Если статус не ясен — 'unknown'.

Текст:
{text[:15000]}"""

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "teacher_extraction",
                    "strict": True,
                    "schema": schema
                }
            },
            temperature=0.1,
            max_tokens=4000  # УВЕЛИЧИЛИ, чтобы 27B модель не обрывала текст
        )
        content = response.choices[0].message.content.strip()

        # На всякий случай чистим markdown (если strict не сработал)
        if content.startswith("```"):
            content = content.split("\n", 1)[1]
            if content.endswith("```"):
                content = content[:-3]
        content = content.strip()

        if not content:
            return None, "Model returned empty string"

        return json.loads(content), None
    except Exception as e:
        return None, str(e)

def save_solution(conn: psycopg.Connection, source_id: int, channel_id: int | None, parsed: dict):
    raw_json = json.dumps(parsed, ensure_ascii=False)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO yt_solution (channel_id, source_id, raw_text, age, region, salary, status, reasons)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_id) DO UPDATE SET
                channel_id = EXCLUDED.channel_id,
                raw_text   = EXCLUDED.raw_text,
                age        = EXCLUDED.age,
                region     = EXCLUDED.region,
                salary     = EXCLUDED.salary,
                status     = EXCLUDED.status,
                reasons    = EXCLUDED.reasons
        """, (
            channel_id, source_id, raw_json,
            parsed.get("age"), parsed.get("region"), parsed.get("salary"),
            parsed.get("status", "unknown"), Jsonb(parsed.get("reasons", []))
        ))
    conn.commit()


def process_llm_queue(conn: psycopg.Connection, args: argparse.Namespace) -> None:
    recover_stuck(conn)
    client = OpenAI(base_url=args.lm_url, api_key="lm-studio", timeout=300.0)

    while True:
        ids = claim_by_status(conn, 'embedded', 'process_solution', args.batch_size)
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
                cur.execute("""
                    SELECT s.contain, cv.channel_id
                    FROM yt_source s
                    LEFT JOIN yt_channel_vids cv ON s.id = cv.source_id
                    WHERE s.id = %s
                """, (sid,))
                row = cur.fetchone()
                if not row or not row[0]:
                    set_status(conn, sid, 'failed_solution', comment='No text found')
                    continue
                text, chan_id = row

            try:
                parsed, error_msg = analyze_text(client, text, args.lm_model)

                if parsed is None:
                    set_status(conn, sid, 'failed_solution', comment=error_msg[:500])
                    logger.error(f"[source_id={sid}] failed: {error_msg}")
                    continue

                save_solution(conn, sid, chan_id, parsed)
                set_status(conn, sid, 'solution')
                logger.info(f"[source_id={sid}] solution saved (status={parsed.get('status')})")
            except Exception as e:
                logger.exception(f"[source_id={sid}] crashed: {e}")
                set_status(conn, sid, 'failed_solution', comment=str(e)[:500])

def main() -> int:
    args = parse_args()
    if not args.process:
        logger.error("Укажите -p для обработки очереди.")
        return 2

    dsn = get_db_dsn(args.host, args.dbname, args.user, args.password, args.port)
    try:
        with connect(dsn) as conn:
            ensure_schema(conn)
            process_llm_queue(conn, args)
    except psycopg.Error as e:
        logger.error(f"Ошибка PostgreSQL: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

