# Utils

Набор вспомогательных скриптов для подготовки тестовых данных и работы с PostgreSQL.

## `download_text.py`

Скрипт для скачивания реальных текстов из датасетов на [Hugging Face](https://huggingface.co/) и сохранения их в CSV. Основное назначение — быстро получить большой объём настоящего текста для тестирования полнотекстового поиска и GIN-индексов в PostgreSQL.

### Что он делает

- Подключается к датасету в режиме **стриминга** — не скачивает весь датасет на диск, а читает ровно столько записей, сколько вы указали.
- Сохраняет результат в CSV с колонками `title`, `author`, `text`.
- Чистит переносы строк в текстах, чтобы CSV корректно читался через `COPY` / `\copy`.
- Принудительно завершает процесс после записи, чтобы фоновые потоки Hugging Face не блокировали выход.

### Требования

- Python 3.10+
- Пакет `datasets` (ставится через `uv`)

```bash
uv pip install datasets
```

### Использование

```bash
uv run python download_text.py [OPTIONS]
```

#### Параметры

| Параметр | По умолчанию | Описание |
|----------|--------------|----------|
| `-l`, `--limit` | `1000` | Сколько записей скачать |
| `-o`, `--output` | `books.csv` | Имя выходного CSV-файла |
| `-d`, `--dataset` | `AdhyanshVerma/pg-en` | Имя датасета на Hugging Face |

#### Примеры

```bash
# Скачать 9 книг в books_9.csv
uv run python download_text.py --limit 9 --output books_9.csv

# Скачать 1000 книг в books_1000.csv
uv run python download_text.py --limit 1000 --output books_1000.csv

# Другой датасет
uv run python download_text.py -d "some-other/dataset" -l 500 -o other.csv
```

### Загрузка в PostgreSQL

После того как CSV готов, создайте таблицу и залейте данные. **Важно:** если PostgreSQL не на локальной машине, используйте `\copy` (клиентская команда psql), а не `COPY` (серверная) — иначе сервер будет искать файл у себя, а не у вас.

#### Шаг 1. Создать таблицу

```sql
CREATE TABLE books (
    id SERIAL PRIMARY KEY,
    title TEXT,
    author TEXT,
    text TEXT
);
```

#### Шаг 2. Залить данные

Из psql в директории, где лежит CSV:

```sql
\copy books(title, author, text) FROM 'books_9.csv' WITH (FORMAT csv, HEADER true)
```

Или с абсолютным путём:

```sql
\copy books(title, author, text) FROM '/<path>/books_9.csv' WITH (FORMAT csv, HEADER true)
```

#### Шаг 3. Собрать статистику

```sql
VACUUM ANALYZE books;
```

### Проверка, что всё загрузилось

```sql
SELECT count(*) FROM books;
SELECT id, title, author, LEFT(text, 100) FROM books LIMIT 5;
```

### Тестирование GIN-индекса

Когда данные загружены, можно собрать `tsvector` и GIN-индекс:

```sql
-- Добавляем колонку с поисковым вектором
ALTER TABLE books ADD COLUMN tsv tsvector
    GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(title, '') || ' ' ||
            coalesce(author, '') || ' ' ||
            coalesce(text, '')
        )
    ) STORED;

-- Строим GIN-индекс
CREATE INDEX idx_books_tsv ON books USING GIN (tsv);

-- Смотрим размер индекса
SELECT
    pg_size_pretty(pg_relation_size('idx_books_tsv')) AS gin_index,
    pg_size_pretty(pg_total_relation_size('books'))    AS total_with_data;

-- Тестируем полнотекстовый поиск
EXPLAIN (ANALYZE, BUFFERS)
SELECT title, author
FROM books
WHERE tsv @@ to_tsquery('english', 'war & peace');
```

### Замечания по производительности

- **TOAST.** Большие тексты автоматически выносятся PostgreSQL в отдельное хранилище TOAST. Поэтому `pg_relation_size('books')` покажет лишь несколько килобайт (там только указатели), а `pg_table_size('books')` — реальный размер вместе с текстами. Это нормально.
- **Стриминг.** Благодаря `streaming=True` и `dataset.take(n)` скрипт читает только нужное количество записей и не пытается скачать весь датасет.
- **Токен Hugging Face.** При больших объёмах (десятки тысяч записей) имеет смысл зарегистрироваться на huggingface.co, получить токен и передать его через переменную окружения — это снимает rate limit:

  ```bash
  HF_TOKEN=ваш_токен uv run python download_text.py --limit 10000
  ```

### Лицензия

Скрипт использует сторонние датасеты с Hugging Face. Перед использованием в продакшене проверяйте лицензию конкретного датасета на его странице.

