FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/app/data/wardenbot.db
WORKDIR /app

COPY requirements.txt ./
COPY wardenbot/requirements.txt ./wardenbot/requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY wardenbot/wardenbot ./wardenbot/wardenbot

RUN mkdir -p /app/data && chown -R 10001:10001 /app
USER 10001:10001
VOLUME ["/app/data"]

CMD ["python", "main.py"]
