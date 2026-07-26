FROM python:3.12-slim

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

EXPOSE ${PORT:-5000}

CMD ["gunicorn", "main:app", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "--access-logfile", "-"]
