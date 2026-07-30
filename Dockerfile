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
#
# fonts-noto-cjk covers Chinese, Japanese and Korean. Those are the one family
# not shipped in assets/fonts: the three Noto CJK files come to about 38 MB,
# which is a fifth again on the desktop installer for languages this is not
# for. Windows and macOS ship their own and libass falls back to them; this
# container ships none, so it takes them from the package manager, where they
# cost the image and nothing else.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-liberation \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY core/ ./core/
# The caption typefaces we ship ourselves. fonts-liberation above covers Arial,
# and nothing on this image covers Nastaliq — so an Urdu caption came out as a
# row of empty boxes, from a render that exits 0 and produces a video nobody can
# post. core/fonts.py looks for exactly this path.
COPY assets/ ./assets/
COPY --from=frontend /build/dist ./frontend/dist

# main.py chdir's here and resolves downloads/ and clips/ relative to it
RUN mkdir -p downloads clips

EXPOSE 8501
CMD uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8501}
