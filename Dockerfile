FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    BUOY_DB=/data/buoy.db

WORKDIR /srv/app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# 单进程单写者：校时重算在进程内锁 + SQLite 事务下保证原子性
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
