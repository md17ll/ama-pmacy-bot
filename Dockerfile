FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && addgroup --system bot \
    && adduser --system --ingroup bot bot

COPY --chown=bot:bot app ./app
COPY --chown=bot:bot main.py ./main.py

RUN python -m compileall -q app main.py \
    && chown -R bot:bot /app

USER bot

CMD ["python", "main.py"]
