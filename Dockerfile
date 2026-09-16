FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY imagebot ./imagebot
COPY start.py ./

# Папка /app/data часто подключается хостингом как том, владельцем которого
# является root. Запуск от root внутри изолированного контейнера позволяет
# SQLite создать базу в таком томе без ручной настройки прав на сервере.
RUN mkdir -p /app/data

CMD ["python", "start.py"]
