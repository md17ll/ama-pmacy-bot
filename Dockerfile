FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY .bootstrap/runtime-b64 /tmp/runtime-b64

RUN python -c "from pathlib import Path; import base64, tarfile; data=b''.join(p.read_bytes() for p in sorted(Path('/tmp/runtime-b64').glob('part-*'))); archive=Path('/tmp/amuda-runtime.tar.xz'); archive.write_bytes(base64.b64decode(data, validate=True)); tarfile.open(archive, 'r:xz').extractall('/app')" \
    && rm -rf /tmp/runtime-b64 /tmp/amuda-runtime.tar.xz

RUN pip install --no-cache-dir -r requirements.txt \
    && python -m compileall -q app main.py

CMD ["python", "main.py"]
