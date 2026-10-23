yt-parser

Пайплайн для сбора, обработки и семантического поиска YouTube-видео на тему увольнений учителей. Собирает метаданные и транскрипты, режет на чанки, считает эмбеддинги — всё складывается в PostgreSQL 18.

Цель: подготовить «сырой» корпус для последующего LLM-извлечения параметров автора (возраст, пол, регион, зарплата, причины увольнения).

🧱 Архитектура

text
Firefox-плагин          `enqueue_api`.py             `load_video`.py -p
    │                        │                           │
    │ POST /enqueue          │ INSERT                    │ claim (SKIP LOCKED)
    ▼                        ▼                           ▼
   [URL] ─────────────► `yt_source` (status='pending') ─────► processing
                                                              │
                                                              ▼
                                          `yt_source`.status = 'ok'/'failed'
                                          `yt_tags` (теги видео)
                                          `yt_chunks` (нарезка транскрипта)
                                                              │
                                                              ▼
                                                    `embed_chunks`.py
                                                    (e5-large, 1024d)
                                                              │
                                                              ▼
                                                    embedding + HNSW
Компоненты:

Файл	Роль
`load_video`.py	Загружает метаданные + транскрипт YouTube → БД. Умеет работать как с одним URL (-y), так и с очередью (-p)
`embed_chunks`.py	Считает эмбеддинги для `yt_chunks` и `yt_source` через sentence-transformers
`enqueue_api`.py	HTTP-мостик: браузерный плагин → Postgres (FastAPI)
yt-queue-plugin/	Firefox-расширение: добавляет текущее YouTube-видео в очередь
(planned) main.py	Веб-морда для мониторинга и запуска задач (FastAPI + Jinja2 + HTMX)
📦 Требования

Python 3.13 (3.12 тоже ок; на 3.15 у torch пока нет wheels)
PostgreSQL 18 с расширениями:

pgvector (для vector(1024), HNSW)
`pg_trgm` (встроено в Postgres, для нечёткого поиска)
uv — менеджер пакетов и окружений
~2.5 GB диска (веса multilingual-e5-large)
macOS / Linux (на Windows нужен WSL для pgvector)
Установка pgvector

macOS (Homebrew Postgres):

bash
brew install pgvector
Кастомная сборка Postgres (например, /Users/<user>/pg18):

bash
git clone --branch v0.8.7 https://github.com/pgvector/pgvector.git
cd pgvector
make
sudo make install
Проверка, что расширение доступно:

sql
SELECT * FROM `pg_available_extensions` WHERE name = 'vector';
Docker:

yaml
image: pgvector/pgvector:pg18
🚀 Установка проекта

bash
git clone <repo> yt-parser
cd yt-parser

# Свой Python-интерпретатор (не 3.15, иначе torch не встанет)
uv python install 3.13
uv python pin 3.13
uv venv
rm -rf .venv && uv venv   # если .venv уже был на 3.15

# Зависимости
uv add yt-dlp youtube-transcript-api "psycopg[binary]"
uv add sentence-transformers
uv add fastapi uvicorn jinja2 python-multipart
Создание БД:

bash
createdb -h localhost ytparse
Схема создаётся автоматически при первом запуске `load_video`.py (CREATE TABLE IF NOT EXISTS, CREATE EXTENSION IF NOT EXISTS).

🗄️ Схема БД

sql
-- Источник: одно YouTube-видео
`yt_source`(
    id SERIAL PK,
    ytlink TEXT UNIQUE,
    metadata JSONB,             -- полный JSON от yt-dlp
    contain TEXT,               -- транскрипт одним куском
    status TEXT,                -- pending|processing|ok|failed
    `error_message` TEXT,
    `contain_tsv` tsvector,       -- русская FTS-нормализация
    embedding vector(1024),     -- усреднение эмбеддингов чанков
    `created_at`, `updated_at`
)

-- Теги (из metadata.tags yt-dlp)
`yt_tags`(
    tag TEXT PK,
    description TEXT,
    weight NUMERIC,
    `created_at`
)

-- Чанки транскрипта (нарезка по словам, ≤4096 байт)
`yt_chunks`(
    id BIGSERIAL PK,
    `source_id` INT FK → `yt_source`(id) ON DELETE CASCADE,
    `chunk_no` INT,
    content TEXT,
    `content_tsv` tsvector,
    embedding vector(1024),
    UNIQUE(`source_id`, `chunk_no`)
)
Индексы:

`yt_source.contain_tsv`, `yt_chunks.content_tsv` → GIN (FTS)
`yt_source`.contain, `yt_chunks`.content → GIN trgm (ILIKE '%...%')
`yt_source`.embedding, `yt_chunks`.embedding → HNSW (cosine)
Порядок вставки: `yt_source` → `yt_chunks`. `yt_tags` независим.

🎬 Использование

Загрузка одного видео

bash
uv run `load_video`.py \
  -l localhost \
  -d ytparse \
  -y "https://www.youtube.com/watch?v=NTAVWjtSuc4"
Флаги:

-l, --host — хост Postgres (default: localhost)
-d, --dbname — БД (default: ytparse)
-u, --user — юзер (default: $PGUSER или OS-юзер)
-w, --password — пароль (default: $PGPASSWORD)
--port — порт (default: 5432)
-y, --url — ссылка (обязательно, если без -p)
Что делает:

yt-dlp → метаданные (title, tags, duration и т.д.)
youtube-transcript-api → транскрипт (ru → en)
UPSERT в `yt_source`
Теги → `yt_tags` (ON CONFLICT DO NOTHING)
Транскрипт режется на чанки → `yt_chunks`
Обработка очереди

bash
# разово: забрать всё pending и выйти
uv run `load_video`.py -d ytparse -p --recover

# демон: ждать новые pending
uv run `load_video`.py -d ytparse -p --watch --recover
Флаги очереди:

-p, --process — режим «разгреби очередь»
--watch — не выходить после пустой очереди, опрашивать каждые N секунд
--poll-interval — интервал поллинга (default: 10)
--batch-size — размер батча claim (default: 10)
--recover — вернуть зависшие processing (старше 30 мин) в pending
Как работает claim: UPDATE ... FOR UPDATE SKIP LOCKED RETURNING id, ytlink. Можно запустить несколько -p --watch параллельно — они не пересекутся.

Эмбеддинги

bash
# только новые чанки (embedding IS NULL)
uv run `embed_chunks`.py -d ytparse

# пересчитать всё (например, после смены модели)
uv run `embed_chunks`.py -d ytparse --force

# тест на 5 чанках
uv run `embed_chunks`.py -d ytparse --limit 5

# другая модель (должна давать 1024d)
uv run `embed_chunks`.py -d ytparse -m BAAI/bge-m3
Флаги:

-m, --model — модель (default: intfloat/multilingual-e5-large)
--device — cpu / mps / cuda (default: auto)
--batch-size — размер батча (default: 32)
--limit — обработать только N чанков
--force — игнорировать WHERE embedding IS NULL
--skip-source — не обновлять `yt_source`.embedding
Офлайн-режим (если модель уже в кэше):

bash
`HF_HUB_OFFLINE`=1 uv run `embed_chunks`.py -d ytparse
Локальные веса (без интернета вообще):

bash
huggingface-cli download intfloat/multilingual-e5-large --local-dir ./models/e5-large
uv run `embed_chunks`.py -d ytparse -m ./models/e5-large
HTTP-API для плагина

bash
uv run uvicorn `enqueue_api`:app --host 127.0.0.1 --port 8765
Эндпоинты:

POST /enqueue {"url": "..."} → {"id": 5, "status": "pending"}
GET /health → {"ok": true, "pending": 3}
Пример:

bash
curl -X POST localhost:8765/enqueue \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.youtube.com/watch?v=NTAVWjtSuc4"}'
Firefox-плагин

text
yt-queue-plugin/
├── manifest.json     # MV3, id=yt-enqueue@local
├── popup.html
└── popup.js          # POST /enqueue
Установка (dev):

about:debugging#/runtime/this-firefox
Load Temporary Add-on… → выбрать manifest.json
Закрепить на панели (правый клик по иконке плагина → Pin)
Требования:

`enqueue_api`.py запущен на http://127.0.0.1:8765
В manifest.json прописан `host_permissions` для этого хоста
Временное расширение исчезает при перезапуске Firefox. Для постоянной установки — подпись через AMO или Developer Edition.

🔍 Примеры запросов

FTS (лексический поиск)

sql
SELECT `chunk_no`,
       `ts_headline`('russian', content,
                   `websearch_to_tsquery`('russian', 'директор'),
                   'StartSel=[, StopSel=]') AS snippet
FROM `yt_chunks`
WHERE `content_tsv` @@ `websearch_to_tsquery`('russian', 'директор')
ORDER BY `chunk_no`;
Найдёт директор, директора, директору — но не руководитель.

Семантический поиск (после `embed_chunks`.py)

python
from `sentence_transformers` import SentenceTransformer
m = SentenceTransformer("intfloat/multilingual-e5-large")
q = m.encode("query: почему учителя уходят", `normalize_embeddings`=True).tolist()
sql
SELECT `chunk_no`, 1 - (embedding <=> %s::vector) AS sim
FROM `yt_chunks`
WHERE embedding IS NOT NULL
ORDER BY embedding <=> %s::vector
LIMIT 5;
Найдёт ушёл, покинул, не могу больше — то, чего FTS не умеет.

Гибридный поиск

Комбинация FTS + вектор через RRF (Reciprocal Rank Fusion) — если понадобится позже. Пока не реализовано.

🖥️ Веб-интерфейс (в планах)

Планируемый стек: FastAPI + Jinja2 + HTMX (без JS-фреймворков).

Функции:

Таблица `yt_source` со статусами (автообновление через hx-trigger="every 3s")
Кнопки «Запустить парсер» / «Запустить эмбеддинги»
Ручной ввод URL (если плагин недоступен)
🐛 Troubleshooting

Симптом	Причина	Решение
torch ... doesn't have wheel for cp315	Python 3.15	uv python pin 3.13 && rm -rf .venv && uv venv
could not open extension control file .../vector.control	pgvector не установлен в инстанс Postgres	make install в pgvector с вашим `pg_config` в PATH
[skip] CREATE EXTENSION vector в логе	Тот же пункт выше	См. раздел «Установка pgvector»
Warning: You are sending unauthenticated requests to HF Hub	Анонимные запросы	Игнорировать, или export `HF_TOKEN`=hf_...
FutureWarning: `get_sentence_embedding_dimension`	Метод переименован	Заменить на `get_embedding_dimension`()
argparse.ArgumentError: conflicting option strings: -y	Дубль -y в `parse_args`()	grep -n '"-y"' `load_video`.py, удалить лишний
Плагин не виден на панели Firefox	Не закреплён	Пазл 🧩 → шестерёнка → Pin to Toolbar
Сеть: Failed to fetch в попапе плагина	`enqueue_api`.py не запущен	uv run uvicorn `enqueue_api`:app --port 8765
Висит processing после падения	Процесс убит до status='ok'	uv run `load_video`.py -p --recover
📂 Структура проекта

text
yt-parser/
├── `load_video`.py             # загрузчик + очередь
├── `embed_chunks`.py           # эмбеддер
├── `enqueue_api`.py            # FastAPI-мостик для плагина
├── main.py                   # (planned) веб-морда
├── pyproject.toml            # зависимости (uv)
├── uv.lock
├── .python-version           # 3.13
├── yt-queue-plugin/          # Firefox-расширение
│   ├── manifest.json
│   ├── popup.html
│   └── popup.js
├── templates/                # (planned) Jinja2
└── static/                   # (planned) CSS
🗺️ Roadmap

☑ Загрузка метаданных и транскрипта в Postgres
☑ Теги из метаданных → `yt_tags`
☑ Чанки транскрипта (`yt_chunks`, ≤4096 байт, без разрыва слов)
☑ Эмбеддинги (e5-large, 1024d), HNSW
☑ Очередь pending → processing → ok/failed
☑ Firefox-плагин + enqueue API
□ LLM-извлечение параметров (возраст, пол, регион, зарплата, причины)
□ Веб-морда (FastAPI + Jinja2 + HTMX)
□ Гибридный поиск (FTS + vector, RRF)
□ Экспорт в CSV / Google Sheets
□ Пакетная обработка десятков видео с прогресс-баром

