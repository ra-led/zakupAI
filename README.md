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
