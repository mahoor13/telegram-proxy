FROM python:3.12-slim

WORKDIR /app

COPY app/requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip==26.1.2 \
    && pip install --no-cache-dir -r requirements.txt

COPY app/ .

EXPOSE 5000

# Telegram bot tokens are part of Bot API paths, so access logging is disabled
# here and should also be redacted/disabled at the front proxy.
CMD ["sh", "-c", "exec gunicorn main:app --bind ${HOST:-0.0.0.0}:${PORT:-5000} --workers 1 --threads ${GUNICORN_THREADS:-8} --timeout ${GUNICORN_TIMEOUT:-1900} --graceful-timeout ${GUNICORN_GRACEFUL_TIMEOUT:-30} --max-requests ${GUNICORN_MAX_REQUESTS:-1000} --max-requests-jitter ${GUNICORN_MAX_REQUESTS_JITTER:-100} --no-control-socket --access-logfile /dev/null --error-logfile -"]
