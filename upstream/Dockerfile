FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 9000

HEALTHCHECK --interval=2s --timeout=2s --start-period=3s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:9000/health')"]

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9000"]
