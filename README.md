# zakupAI

Простейший сервис по описанию из `AGENTS.md`: FastAPI backend + React/Vite фронтенд. Закрывает базовые сценарии MVP: управление закупками, списками поставщиков и контактами, заготовки для LLM-задач, работа с шаблонами писем и учёт ящиков пользователя.

## Быстрый старт через docker-compose
1. Скопируйте переменные окружения и при необходимости отредактируйте:
   ```bash
   cp .env.example .env
   ```
2. Поднимите стек (PostgreSQL + backend + фронтенд + nginx-прокси):
   ```bash
   docker-compose up --build
   ```
3. Через nginx фронтенд доступен на http://localhost, API — на http://localhost/api (Swagger: `/api/docs`). Для отладки можно ходить напрямую на backend http://localhost:8000.

## Деплой в Coolify
Для Coolify подготовлены отдельные файлы:
- `docker-compose.coolify.yml`
- `nginx.coolify.conf`

Что важно для Coolify:
- Внутри контейнера `nginx` работает только по `80` (TLS и сертификаты терминирует сам Coolify).
- В качестве публичного сервиса в Coolify укажите `nginx` на порту `80`.
- В переменных окружения обязательно задайте:
  - `DATABASE_URL` (обычно `postgresql+psycopg2://...@db:5432/...`)
  - `CORS_ORIGINS` (например, `https://ваш-домен`)
  - ключи интеграций (`OPENAI_API_KEY`/`OPENROUTER_API_KEY`, `YANDEX_API_KEY`, `YANDEX_FOLDER_ID` и т.д.)
- `VITE_API_URL` можно оставить пустым, тогда фронтенд будет ходить в API через текущий домен и путь `/api`.

Основные переменные `.env` для поиска поставщиков:
- `YANDEX_API_KEY`, `YANDEX_FOLDER_ID` — ключ и каталог Yandex Search API.
- `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_BASE_URL` — доступ к LLM для генерации запросов/валидации.
- `PAGE_LOAD_TIMEOUT`, `QUERY_DOCS_LIMIT` — таймаут загрузки страниц и лимит релевантных документов на запрос.
- `ETL_POLL_INTERVAL` — частота опроса очереди воркером; `ENABLE_EMBEDDED_QUEUE=false` оставляет обработку только за сервисом `etl`.

## Локальный запуск backend (без Docker)
1. Установите зависимости
   ```bash
   pip install -r requirements.txt
   ```
2. Укажите строку подключения к БД (например, PostgreSQL в Docker):
   ```bash
   export DATABASE_URL="postgresql+psycopg2://zakupai:zakupai@localhost:5432/zakupai"
   export CORS_ORIGINS="http://localhost:4173,http://localhost:3000"
   ```
   Если переменная не задана, используется локальный SQLite-файл `database.db`.
3. Запустите сервер
   ```bash
   uvicorn app.main:app --reload
   ```
4. Откройте интерактивную документацию по адресу http://127.0.0.1:8000/docs

## Локальная разработка фронтенда
1. Перейдите в каталог `frontend` и установите зависимости (Node 18+):
   ```bash
   cd frontend
   npm install
   ```
2. Запустите Vite dev server с пробросом API-адреса (по умолчанию http://localhost:8000):
   ```bash
   npm run dev -- --host 0.0.0.0 --port 4173
   ```
3. Для сборки production-версии выполните `npm run build`.

## Основные возможности API
- Регистрация и вход по email/паролю (`/auth/register`, `/auth/login`).
- Управление закупками: создание, просмотр, обновление статуса и НМЦК.
- Ведение списка поставщиков и их email-контактов для каждой закупки.
- Хранение почтовых настроек пользователя и истории исходящих/входящих писем.
- Создание заготовок LLM-задач и генерация поисковых запросов по ТЗ без обращения к внешним API.
- Автогенерация черновика письма-запроса КП на основе закупки и выбранного поставщика.
- Автоматическая постановка задач на поиск поставщиков: после создания закупки формируется очередь, которую обрабатывает отдельный ETL-воркер с реальным парсингом email-адресов.

### Промышленный ETL для поиска контактов
- В стеке `docker-compose` добавлен сервис `etl`, который использует `suppliers_contacts.py` для реального поиска сайтов, валидации и извлечения email-адресов (через Yandex Search API + headless Chromium).
- После создания закупки backend ставит задачу `supplier_search` в таблице `LLMTask`, а воркер `etl` забирает их по одной, сохраняет найденные сайты, создаёт поставщиков и контакты в БД и пишет полный JSON результата в `LLMTask.output_text`.
- Запрос `POST /purchases/{purchase_id}/suppliers/search` теперь возвращает не только статус, но и детализированные `processed_contacts` и `search_output` (emails), которые уже лежат в базе.
- Для ручного прогона скрипта можно вызвать `python suppliers_contacts.py`; результаты попадут в `processed_contacts.json` и `search_output.json` и также могут быть импортированы через `/suppliers/import-script-output`.

### Очередь авто-поиска поставщиков
- При создании закупки автоматически ставится задача `supplier_search`, в `LLMTask.input_text` сохраняется техническое задание.
- Фоновый воркер (`app.task_queue`) последовательно берёт задачи из БД, строит поисковые запросы и записывает результат в `LLMTask.output_text`.
- Статус и подготовленные запросы доступны через `POST /purchases/{purchase_id}/suppliers/search` (возвращает id задачи, статус и подготовленные запросы).

## Production deploy

Деплой в прод (https://app.zakupai.tech) автоматический через `.github/workflows/deploy.yml` на каждый push в `main`.

### Защита от тихих провалов

После инцидента 2026-04-11 (Docker layer cache + молчаливый CD оставили live сайт со старым кодом 4 коммита подряд) пайплайн состоит из 6 шагов и **внешне верифицирует результат**:

1. **validate / Python syntax check** — `py_compile` всего `app/` и `etl/`
2. **validate / JavaScript syntax check** — `node --check` всего `frontend/js/`
3. **validate / HTML smoke** — `index.html` существует и ссылается на `app.js`
4. **deploy / Deploy via SSH:**
   - `git fetch + git reset --hard ${SHA}` (НЕ `git pull` — может молча провалиться)
   - Инжект `frontend/build.txt` с `${GIT_SHA} ${TIMESTAMP}` (gitignored, попадает в образ через `COPY .`)
   - `docker compose build --no-cache frontend` (ОБЯЗАТЕЛЬНО — Docker BuildKit имеет привычку возвращать кэшированный COPY-слой; cost 2 секунды)
   - `docker compose up -d --force-recreate --no-deps frontend` (ОБЯЗАТЕЛЬНО — иначе compose не пересоздаёт контейнер если digest не сменился)
5. **deploy / Verify live frontend serves the new SHA** — `curl https://app.zakupai.tech/build.txt?cb=$(date +%s)` с `Cache-Control: no-cache`, retry 10×3s, fail если SHA не совпал
6. **deploy / Verify backend is healthy** — `curl https://app.zakupai.tech/api/health`, retry 5×3s

**Зелёный workflow = пользователь видит этот коммит на проде и backend жив.** Не «команды отработали».

### Локальная проверка что задеплоено

```bash
curl -s "https://app.zakupai.tech/build.txt?cb=$(date +%s)"
# вернёт: <SHA> <ISO timestamp>

curl -s https://app.zakupai.tech/api/health
# {"status":"ok"}
```

### Self-service диагностика

В UI рядом со статусом «Лоты» есть кнопка **Диагностика** — открывает модалку с дампом `GET /purchases/{id}/lots/diagnostics`. Структура ответа:

1. **`summary`** — сводка по подсистемам (`lots`, `supplier_search`, `infrastructure`) с verdict-ами (`ok`/`running`/`stuck`/`failed`/`idle`/`broken`), human-readable статусом с эмодзи, и `action_hint` для пользователя. Это первое что нужно читать.
2. **`summary.supplier_search.crawl_progress`** — `{processed, total, percent}` для краулинга сайтов. UI рисует ASCII прогресс-бар.
3. **`lots_tasks` / `supplier_tasks` / `other_tasks`** — раздельные секции с задачами. Каждая задача имеет `age_seconds`, `seconds_since_update`, `note`, `error` (предпарсенные из ПОЛНОГО `output_text`, не из truncated preview).
4. Сырой JSON для копирования через кнопку «Скопировать JSON».

В модалке есть кнопки **«Сбросить распознавание»** и **«Сбросить поиск поставщиков»** (POST `/purchases/{id}/tasks/reset?task_type=X`) для принудительного fail-а зависших задач.

### Long-running pipelines: incremental progress

ETL `_collect_combined_contacts` принимает `progress_cb` и пишет partial-результат в `LLMTask.output_text` после каждого этапа (Yandex done, Perplexity done, найдено N сайтов, обход выполнен). `collect_contacts_from_websites` дополнительно дёргает callback после **каждого сайта** с ETA — формат note `"Краулинг сайтов: 12/47 (текущий: example.com, осталось ~10м)"`. Diagnostics regex-парсит эту строку и возвращает `crawl_progress: {processed, total, percent}` для UI.

`LLMTask.updated_at` (миграция в `_ensure_llmtask_columns`) тикает на каждом write — diagnostics возвращает `seconds_since_update` чтобы отличить «работает медленно» от «умер 30 минут назад». Помечает задачу `⚠ ВОЗМОЖНО ЗАВИСЛА` если `seconds_since_update > 120`.

### Recovery loops при деплоях

ETL `_recover_stale_tasks` различает recoverable (`age < 30 min`) и abandoned (`age > 30 min`) задачи. Без этого долгий пайплайн (10-15 мин) застревал в бесконечном цикле при частых деплоях: каждый деплой → restart → recovery requeue → start over. Сейчас abandoned задачи помечаются `failed` с понятным сообщением, требуется ручной retry.

Для коротких задач (lots_extraction, ~30s) аналогичная защита в `task_queue.enqueue_lots_extraction_task` с порогом 5 минут — освобождает enqueue от зависших in_progress.

### Кэш-стратегия nginx

`frontend/nginx.conf`:
- `*.html` и SPA fallback → `Cache-Control: no-store, no-cache, must-revalidate` — никогда не кэшируется (иначе пользователи висят на старых script-тегах)
- `*.css/*.js` → `Cache-Control: no-cache, must-revalidate + etag` — query-string busts работают, но браузер всё равно ревалидирует
- картинки/шрифты → `expires 7d immutable` (они не меняются)

`immutable` cache подходит ТОЛЬКО для версионированных URL (`app.abc123.js`). Для статичных имён — всегда `no-cache + etag`.

## Быстрый сценарий через cURL
```bash
# регистрация
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"user@example.com","password":"secret12"}'

# вход и получение токена
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"user@example.com","password":"secret12"}' | jq -r .token)

# создание закупки
curl -X POST http://localhost:8000/purchases \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"custom_name":"Шины","terms_text":"Поставка шин для грузовых авто"}'
```
