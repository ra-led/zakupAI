# zakupAI

Простейший сервис по описанию из `DESIGN.md`: FastAPI backend + React/Vite фронтенд. Закрывает базовые сценарии MVP: управление закупками, списками поставщиков и контактами, заготовки для LLM-задач, работа с шаблонами писем и учёт ящиков пользователя.

## Быстрый старт через docker-compose
1. Скопируйте переменные окружения и при необходимости отредактируйте:
   ```bash
   cp .env.example .env
   ```
2. Поднимите стек (PostgreSQL + backend + фронтенд):
   ```bash
   docker-compose up --build
   ```
3. Фронтенд доступен на http://localhost:4173, backend — на http://localhost:8000 (Swagger: `/docs`).

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
