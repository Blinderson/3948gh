FROM python:3.12-slim

WORKDIR /app

COPY alarm.py /app/alarm.py
COPY users.db /app/users.db

# Установка зависимостей
RUN pip install --no-cache-dir aiogram==3.13.1 aiohttp aiosqlite

CMD ["python", "alarm.py"]
