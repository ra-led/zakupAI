FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app app
COPY suppliers_contacts.py suppliers_contacts.py
COPY DESIGN.md DESIGN.md

ENV DATABASE_URL=postgresql+psycopg2://zakupai:zakupai@db:5432/zakupai
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
