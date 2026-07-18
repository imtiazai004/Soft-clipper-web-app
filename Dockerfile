# ── stage 1: build the React frontend ────────────────────────────────────────
FROM node:22-slim AS frontend

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


# ── stage 2: the app ─────────────────────────────────────────────────────────
FROM python:3.12-slim

# ffmpeg does the cutting/encoding; fonts-liberation is what makes burned-in
# captions readable — captions.py asks for Arial, which no Linux box has, and
# fontconfig maps it to Liberation Sans. Without this, text renders as blanks.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY core/ ./core/
COPY --from=frontend /build/dist ./frontend/dist

# main.py chdir's here and resolves downloads/ and clips/ relative to it
RUN mkdir -p downloads clips

EXPOSE 8501
CMD uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8501}
