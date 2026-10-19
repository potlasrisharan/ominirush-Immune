FROM python:3.12.8-slim

WORKDIR /app

COPY requirements.lock ./requirements.lock
RUN python -m pip install --no-cache-dir -r requirements.lock

COPY omniimmune ./omniimmune
COPY target_app ./target_app
COPY upstream ./upstream
COPY .env.example ./

ENV PYTHONUNBUFFERED=1
ENV UPSTREAM_URL=http://upstream:9000
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "target_app.app:app", "--host", "0.0.0.0", "--port", "8000"]
