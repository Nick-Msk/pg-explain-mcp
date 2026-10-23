# Changelog

Все значимые изменения проекта. Формат по мотивам
[Keep a Changelog](https://keepachangelog.com/), версионирование — по датам.

---

## [2026-10-23] — Эмбеддинги, очередь, плагин

### Added
- `embed_chunks.py` — заполнение `yt_chunks.embedding` и `yt_source.embedding`
  через `sentence-transformers` (`intfloat/multilingual-e5-large`, 1024d).
  - Инкрементальный режим (`WHERE embedding IS NULL`), флаг `--force`.
  - `yt_source.embedding` = усреднение чанков (`AVG(vector)`), только если все
    чанки источника имеют эмбеддинг (`HAVING COUNT(...) = 0`).
  - Флаг `--limit` для тестов, `--skip-source`, `--device` (mps/cuda/cpu).
  - Поддержка `HF_HUB_OFFLINE=1` и локальных весов (`-m ./models/...`).
- `enqueue_api.py` — HTTP-мостик на FastAPI для браузерного плагина.
  - `POST /enqueue` — принять URL, вставить `status='pending'`, вернуть id.
  - `GET /health` — счётчик pending.
- `yt-queue-plugin/` — Firefox-расширение (MV3) для добавления текущего
  YouTube-видео в очередь.
- Очередь в `load_video.py`:
  - `-p / --process` — режим обработки очереди.
  - `--watch` — демон-поллинг.
  - `--batch-size` — размер батча claim (default: 10).
  - `--poll-interval` — интервал поллинга (default: 10 сек).
  - `--recover` — возврат зависших `processing` (>30 мин) в `pending`.
  - `claim_pending()` через `UPDATE ... FOR UPDATE SKIP LOCKED RETURNING`.
  - `recover_stuck()`, `mark_failed()`.
- CLI: `-y/--url` теперь опциональный (`default=None`), чтобы работал `-p`.

### Changed
- `save_to_db()` возвращает `source_id` (`RETURNING id`) — нужно для связи
  с `yt_chunks`.
- `process_video()` вызывает `save_chunks()` после успешного сохранения
  источника. Теги сохраняются отдельно и не роняют процесс при ошибке.

---

## [2026-10-22] — Схема, чанки, индексы

### Added
- Схема БД:
  - `yt_source` (id, ytlink, metadata JSONB, contain, status, `error_message`,
    `contain_tsv` tsvector, embedding vector(1024), timestamps).
  - `yt_tags` (tag PK, description, weight, `created_at`).
  - `yt_chunks` (id, `source_id` FK, `chunk_no`, content, `content_tsv` tsvector,
    embedding vector(1024), timestamps).
- Расширения: `vector`, `pg_trgm` (best-effort `CREATE EXTENSION`).
- Индексы:
  - GIN на `contain_tsv` / `content_tsv` (FTS, `russian`).
  - GIN trgm на `contain` / `content` (быстрый `ILIKE '%...%'`).
  - HNSW на `embedding` (`vector_cosine_ops`).
- Генерируемые колонки `contain_tsv` / `content_tsv` через
  `GENERATED ALWAYS AS (to_tsvector('russian', ...)) STORED`.
- Функция `chunk_text(text, max_bytes=4096)` — нарезка без разрыва слов,
  учёт UTF-8 байт (кириллица ≈ 2 байта/символ).
- Функция `save_chunks()` — идемпотентная перезапись (`DELETE` + `INSERT`
  в одной транзакции).
- `ensure_schema()` — per-statement `try/except` с `rollback`, чтобы отсутствие
  `pgvector` не роняло создание базовых таблиц.
- CLI `load_video.py`: `-l/--host`, `-d/--dbname`, `-u/--user`, `-w/--password`,
  `--port`, `-y/--url`.

### Notes
- Первый прогон `embed_chunks.py --limit 5` проверил пайплайн на 5 чанках.
- Полный прогон (33 оставшихся чанка) обновил `yt_source.embedding` для 2 видео.
- На 38 строках в `yt_chunks` планировщик выбирает `Seq Scan` — GIN включается
  при сотнях-тысячах строк. Проверено через `EXPLAIN (ANALYZE)`.
- `pgvector` установлен из исходников (`make && sudo make install`) с
  правильным `pg_config` в `PATH` (кастомная сборка Postgres в
  `/Users/postgres/pg18`).

---

## [2026-10-21] — MVP загрузчика

### Added
- `load_video.py` — загрузка метаданных через `yt-dlp` и транскрипта через
  `youtube-transcript-api`.
- Извлечение `video_id` из разных форматов URL (`watch?v=`, `youtu.be/`,
  `shorts/`).
- Нормализация URL (`www.youtube.com/...` → `https://www.youtube.com/...`).
- UPSERT в `yt_source` через `ON CONFLICT (ytlink) DO UPDATE`.
- Обработка ошибок: при падении метаданных или транскрипта запись сохраняется
  со `status='failed'` и `error_message`.

### Notes
- Первый тест на `https://www.youtube.com/watch?v=NTAVWjtSuc4` — 38 чанков,
  14 тегов.

---

## TODO (следующая итерация)

- [ ] LLM-извлечение параметров автора:
  - возраст на момент увольнения
  - пол
  - рост / вес (если упомянуты)
  - регион
  - зарплата
  - одна или несколько причин увольнения
  - `is_resigned` (уволился / собирается)
- [ ] Веб-морда (FastAPI + Jinja2 + HTMX): статусы, кнопки запуска.
- [ ] Гибридный поиск (FTS + vector через RRF).
- [ ] Экспорт в CSV / Google Sheets.
- [ ] Тонкая настройка промптов для LLM (constrained decoding / Outlines).

