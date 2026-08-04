FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY .bootstrap/project.zip /tmp/amuda-project.zip
RUN unzip -o /tmp/amuda-project.zip -d /app \
    && rm /tmp/amuda-project.zip

RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "main.py"]
