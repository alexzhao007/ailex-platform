FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn pydantic httpx

COPY secure_service.py .

RUN mkdir -p /app/data

EXPOSE 8093

CMD ["uvicorn", "secure_service:app", "--host", "0.0.0.0", "--port", "8093"]
