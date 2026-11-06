FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5000

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py change-node-ip.sh /app/
RUN chmod +x /app/change-node-ip.sh

EXPOSE 5000

CMD ["gunicorn", "--workers", "1", "--threads", "4", "--bind", "0.0.0.0:5000", "--timeout", "60", "app:app"]
