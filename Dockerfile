# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=0 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# FFmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt ./
RUN python -m pip install --upgrade pip && \
    pip install --prefer-binary -r requirements.txt

# App code
COPY . .

CMD ["python", "bot.py"]
