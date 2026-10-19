FROM python:3.12.8-slim

WORKDIR /app

COPY requirements.lock ./requirements.lock
RUN python -m pip install --no-cache-dir -r requirements.lock

COPY . .

ENV PYTHONUNBUFFERED=1
ENV UPSTREAM_URL=http://upstream:9000
CMD ["python", "main.py", "--mode", "docker", "--no-ui"]
