# --- Этап 1: сборка интерфейса ----------------------------------------------------------
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
# Скрипты установки не нужны ни одному из пакетов — отключаем их из осторожности
RUN npm ci --ignore-scripts
COPY frontend/ ./
RUN npm run build

# --- Этап 2: backend вместе с браузером для Playwright ----------------------------------
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1
WORKDIR /app/backend

# Сначала зависимости: слой кешируется, пока не изменились pyproject.toml и uv.lock
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev
ENV PATH="/app/backend/.venv/bin:$PATH"

# Chromium и системные библиотеки для него; версия браузера берётся из версии пакета playwright
RUN playwright install --with-deps chromium

COPY backend/app ./app
COPY --from=frontend /frontend/dist /app/frontend/dist

ENV DATA_DIR=/app/data \
    FRONTEND_DIST=/app/frontend/dist
VOLUME /app/data
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
