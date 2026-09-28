# syntax=docker/dockerfile:1

# --- frontend build -------------------------------------------------------
FROM node:20-alpine AS frontend
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- backend + TeX + fonts ------------------------------------------------
FROM python:3.12-slim AS app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends \
      poppler-utils \
      texlive-xetex texlive-latex-base texlive-latex-recommended \
      texlive-latex-extra texlive-fonts-recommended \
      fonts-noto-core fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend
COPY backend/pyproject.toml ./
COPY backend/ocrtran ./ocrtran
COPY backend/app ./app
RUN pip install ".[server]"

# serve the built SPA from FastAPI (main.py looks at ../../frontend/dist)
COPY --from=frontend /web/dist /app/frontend/dist

ENV STORAGE_DIR=/data
VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
