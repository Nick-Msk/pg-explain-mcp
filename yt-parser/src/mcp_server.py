import json

from fastmcp import FastMCP
from openai import OpenAI
from psycopg.types.json import Jsonb

from src.db import connect, get_db_dsn

mcp = FastMCP(name="yt-param-extractor")
DB_DSN = get_db_dsn()

# Клиент для LM Studio
client = OpenAI(base_url="http://127.0.0.1:1234/v1", api_key="lm-studio")

@mcp.tool
def get_unprocessed_sources(limit: int = 10) -> list[dict]:
    """Возвращает список ID видео, для которых ещё нет анализа."""
    with connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.id, s.ytlink 
                FROM yt_source s
                LEFT JOIN yt_solution sol ON s.id = sol.source_id
                WHERE s.status = 'ok' AND sol.id IS NULL
                LIMIT %s
            """, (limit,))
            return [{"source_id": r[0], "ytlink": r[1]} for r in cur.fetchall()]

@mcp.tool
def extract_and_save(source_id: int) -> dict:
    """Извлекает параметры через LM Studio и сохраняет в yt_solution."""
    with connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.contain, cv.channel_id 
                FROM yt_source s
                LEFT JOIN yt_channel_vids cv ON s.id = cv.source_id
                WHERE s.id = %s
            """, (source_id,))
            row = cur.fetchone()
            if not row or not row[0]:
                return {"error": "Видео или транскрипт не найдены"}
            text, chan_db_id = row

    prompt = f"""Проанализируй текст. Верни СТРОГО JSON.
Если информация не упоминается или ты не уверен — ставь null (или 'unknown' для status).
Поля:
- age (int|null)
- region (str|null)
- salary (str|null)
- status (str) - одно из: 'working', 'resigned', 'fired', 'in_thinking', 'unknown'
- reasons (list[str])
- summary (str) - краткая выжимка

Текст:
{text[:12000]}"""

    try:
        response = client.chat.completions.create(
            model="local-model",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.1
        )
        parsed = json.loads(response.choices[0].message.content)
    except Exception as e:
        return {"error": f"Ошибка LM Studio: {str(e)}"}

    with connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO yt_solution (channel_id, source_id, raw_text, age, region, salary, status, reasons)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_id) DO UPDATE SET
                    age = EXCLUDED.age, region = EXCLUDED.region,
                    salary = EXCLUDED.salary, status = EXCLUDED.status,
                    reasons = EXCLUDED.reasons, raw_text = EXCLUDED.raw_text
            """, (
                chan_db_id, source_id, response.choices[0].message.content,
                parsed.get("age"), parsed.get("region"), parsed.get("salary"),
                parsed.get("status", "unknown"), Jsonb(parsed.get("reasons", []))
            ))
        conn.commit()
    return parsed

if __name__ == "__main__":
    mcp.run()

