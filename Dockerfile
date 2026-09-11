FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY returnguard returnguard
COPY sql sql
ENV PORT=8080
CMD exec uvicorn returnguard.api.app:app --host 0.0.0.0 --port ${PORT}
