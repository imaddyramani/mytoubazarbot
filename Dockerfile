FROM python:3.11-slim-bookworm

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV OMP_THREAD_LIMIT=1
ENV OMP_NUM_THREADS=1
ENV MALLOC_ARENA_MAX=2
ENV LOCAL_WHISPER_MODEL=tiny
ENV WHISPER_CACHE_DIR=/app/.cache/whisper

# System libraries required by WeasyPrint/Pango + common PDF/image/font operations.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gcc \
    libcairo2 \
    libffi8 \
    libfontconfig1 \
    libfreetype6 \
    libglib2.0-0 \
    libgl1 \
    libgomp1 \
    libgdk-pixbuf-2.0-0 \
    libharfbuzz0b \
    libharfbuzz-subset0 \
    libjpeg62-turbo \
    libopenjp2-7 \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libpng16-16 \
    poppler-utils \
    fonts-dejavu-core \
    fonts-liberation \
    fonts-noto-core \
    fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt

RUN python -m pip install --root-user-action=ignore --no-cache-dir --upgrade pip && \
    python -m pip install --root-user-action=ignore --no-cache-dir -r /app/requirements.txt

# Cache the multilingual voice model during image build. Telegram voice notes then
# work immediately without a large first-request download or Northflank timeout.
RUN mkdir -p /app/.cache/whisper && \
    python -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cpu', compute_type='int8', download_root='/app/.cache/whisper')"

COPY . /app

# Runtime folders are writable and available even if Git doesn't keep empty folders.
RUN mkdir -p \
    /app/data/generated \
    /app/data/incoming \
    /app/data/records \
    /app/temp \
    /app/tmp

# Fail the image build immediately if the PDF stack itself is broken.
RUN python -c "from weasyprint import HTML; b=HTML(string='<html><body>PDF engine OK</body></html>').write_pdf(); assert len(b) > 100"

EXPOSE 8080

CMD ["python", "start.py"]
