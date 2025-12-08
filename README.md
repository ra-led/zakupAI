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
- Автоматическая постановка задач на поиск поставщиков: после создания закупки формируется очередь, фоновый воркер строит поисковые запросы по тексту ТЗ и сохраняет результат в историю задач.

### Импорт email-контактов из `suppliers_contacts.py`
1. Выполните внешнюю утилиту `suppliers_contacts.py` (в неё уже встроены примеры LLM-вызовов и парсинга). Она сохранит `processed_contacts.json` и `search_output.json` рядом с проектом.
2. Импортируйте результат в закупку через API (можно указывать пути к json или передавать сами массивы):
   ```bash
   curl -X POST http://localhost:8000/purchases/1/suppliers/import-script-output \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"processed_contacts_path":"processed_contacts.json","search_output_path":"search_output.json"}'
   ```
   Сервис объединит контакты и emails из выводов `suppliers_contacts.py`, создаст поставщиков/контакты и вернёт количество добавленных записей.

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
