FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/

EXPOSE 8420

# Flask app served via gunicorn (server.py defines `app`)
CMD ["gunicorn", "-b", "0.0.0.0:8420", "-w", "2", "--timeout", "120", "server:app"]
