FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY src/ ./src/
COPY static/ ./static/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_LEVEL=INFO

EXPOSE 5000

# gunicorn with 1 worker — the queue lives in-process and spawns its own threads
# Multiple workers would mean parallel queues + duplicate DB writes. Don't scale with workers.
CMD gunicorn -w 1 -k gthread --threads 8 -b 0.0.0.0:${PORT:-5000} \
    --timeout 600 --graceful-timeout 30 \
    --access-logfile - --error-logfile - \
    app:app
