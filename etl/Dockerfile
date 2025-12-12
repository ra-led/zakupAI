FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

WORKDIR /app

RUN apt-get update \ 
    && apt-get install -y --no-install-recommends chromium chromium-driver \ 
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
COPY etl/requirements.txt ./etl-requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r etl-requirements.txt

COPY . .

CMD ["python", "-m", "etl.worker"]
