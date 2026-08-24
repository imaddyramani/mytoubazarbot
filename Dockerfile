FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    gcc \
    libjpeg62-turbo \
    libfreetype6 \
    libpng16-16 \
    libglib2.0-0 \
    libgl1 \
    poppler-utils \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --root-user-action=ignore --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["sh", "-c", "python -m http.server ${PORT:-8080} >/dev/null 2>&1 & exec python bot.py"]
