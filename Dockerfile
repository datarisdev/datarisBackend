FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libpq-dev \
    gdal-bin \
    libgdal-dev \
    proj-bin \
    libproj-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./

RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY api ./api
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY scripts ./scripts

CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1
