#!/usr/bin/env python3
"""
Анализ видео через LM Studio и заполнение yt_solution.
Запуск: uv run src/exec_llm.py -d ytparse -p
"""

import argparse
import json
import logging
import os
import sys

import psycopg
from openai import OpenAI
from psycopg.types.json import Jsonb

# Импорты из соседних файлов в src/
from db import connect, get_db_dsn

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("exec_llm")

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Заполняет yt_solution через LM Studio.")
    p.add_argument("-l", "--host", default="localhost")
    p.add_argument("-d", "--dbname", default="ytparse")
    p.add_argument("-u", "--user", default=os.getenv("PGUSER"))
    p.add_argument("-w", "--password", default=os.getenv("PGPASSWORD"))
    p.add_argument("--port", type=int, default=int(os.getenv("PGPORT", "5432")))

    p.add_argument("-p", "--process", action="store_true", help="Обработать все новые видео")
    p.add_argument("--limit", type=int, default=None, help="Максимум видео для обработки")
    p.add_argument("--force", action="store_true", help="Перезаписать уже существующие yt_solution")

    p.add_argument("--lm-url", default="http://127.0.0.1:1234/v1", help="URL LM Studio")
    p.add_argument("--lm-model", default="local-model", help="Имя модели (LM Studio игнорирует)")
    return p.parse_args()

def get_videos_to_process(conn: psycopg.Connection, limit: int | None, force: bool) -> list[tuple]:
    """Берёт видео, для которых еще нет yt_solution."""
    sql = """
        SELECT s.id, s.contain, cv.channel_id
        FROM yt_source s
        LEFT JOIN yt_channel_vids cv ON s.id = cv.source_id
        LEFT JOIN yt_solution sol ON s.id = sol.source_id
        WHERE s.status = 'ok' AND s.contain IS NOT NULL
    """
    if not force:
        sql += " AND sol.id IS NULL"

    sql += " ORDER BY s.id"
    if limit:
        sql += f" LIMIT {int(limit)}"

    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()

def analyze_text(client: OpenAI, text: str, model: str) -> dict:
    prompt = f"""Ты — аналитик. Проанализируй текст интервью учителя.
Верни СТРОГО JSON.
Поля:
- age (int|null) - возраст на момент увольнения
- region (str|null) - регион/город
- salary (str|null) - зарплата, как указано в тексте
- status (str) - одно из: 'working', 'resigned', 'fired', 'in_thinking', 'unknown'
- reasons (list[str]) - список причин увольнения
- raw_text (str) - краткая выжимка (2-3 предложения)

Если данных нет — ставь null. Если статус не ясен — 'unknown'.

Текст:
{text[:15000]}"""

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=1000
        )
        content = response.choices[0].message.content
        return json.loads(content)
    except Exception as e:
        logger.error(f"Ошибка LLM: {e}")
        return None

def save_solution(conn: psycopg.Connection, source_id: int, channel_id: int | None, parsed: dict, raw_json: str):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO yt_solution (channel_id, source_id, raw_text, age, region, salary, status, reasons)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_id) DO UPDATE SET
                channel_id = EXCLUDED.channel_id,
                raw_text = EXCLUDED.raw_text,
                age = EXCLUDED.age,
                region = EXCLUDED.region,
                salary = EXCLUDED.salary,
                status = EXCLUDED.status,
                reasons = EXCLUDED.reasons
        """, (
            channel_id, source_id, raw_json,
            parsed.get("age"), parsed.get("region"), parsed.get("salary"),
            parsed.get("status", "unknown"), Jsonb(parsed.get("reasons", []))
        ))
    conn.commit()

def main():
    args = parse_args()
    dsn = get_db_dsn(args.host, args.dbname, args.user, args.password, args.port)

    client = OpenAI(base_url=args.lm_url, api_key="lm-studio")

    try:
        with connect(dsn) as conn:
            videos = get_videos_to_process(conn, args.limit, args.force)
            if not videos:
                logger.info("Нет новых видео для анализа.")
                return 0

            logger.info(f"К обработке: {len(videos)} видео.")
            for idx, (source_id, text, channel_id) in enumerate(videos, 1):
                logger.info(f"[{idx}/{len(videos)}] Анализ source_id={source_id}...")
                parsed = analyze_text(client, text, args.lm_model)
                if parsed is None:
                    logger.warning(f"source_id={source_id}: пропущено из-за ошибки LLM.")
                    continue

                # Сохраняем сырой JSON и распарсенные поля
                raw_json = json.dumps(parsed, ensure_ascii=False)
                save_solution(conn, source_id, channel_id, parsed, raw_json)
                logger.info(f"source_id={source_id}: сохранено (status={parsed.get('status')}).")

    except psycopg.Error as e:
        logger.error(f"Ошибка БД: {e}")
        return 1
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        return 1

    return 0

if __name__ == "__main__":
    sys.exit(main())

