#!/usr/bin/env python3
"""Мини-API для enqueue YouTube-ссылок из браузерного расширения."""

import os
import re

import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DB_DSN = os.getenv(
    "DB_DSN",
    "postgresql://localhost:5432/ytparse",
)
YT_RE = re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([0-9A-Za-z_-]{11})")

app = FastAPI(title="yt-enqueue")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # для личного инструмента ок; для прода — ограничить
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class EnqueueReq(BaseModel):
    url: str


def normalize(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")
    return url


@app.post("/enqueue")
def enqueue(req: EnqueueReq):
    url = normalize(req.url)
    if not YT_RE.search(url):
        raise HTTPException(400, "Не YouTube-видео")

    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO yt_source (ytlink, status)
                VALUES (%s, 'pending')
                ON CONFLICT (ytlink) DO NOTHING
                RETURNING id
                """,
                (url,),
            )
            row = cur.fetchone()
            if row:
                conn.commit()
                return {"id": row[0], "status": "pending",
                        "message": f"Добавлено в очередь (id={row[0]})"}

            cur.execute("SELECT id, status FROM yt_source WHERE ytlink = %s", (url,))
            existing = cur.fetchone()
        conn.commit()
    return {"id": existing[0], "status": existing[1],
            "message": f"Уже в БД: status={existing[1]}"}


@app.get("/health")
def health():
    with psycopg.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM yt_source WHERE status='pending'")
        pending = cur.fetchone()[0]
    return {"ok": True, "pending": pending}

