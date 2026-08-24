FROM python:3.11-slim-bookworm

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

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
