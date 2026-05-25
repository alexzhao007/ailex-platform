FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn pydantic httpx

COPY learn_service.py .

RUN mkdir -p /app/data

EXPOSE 8092

CMD ["uvicorn", "learn_service:app", "--host", "0.0.0.0", "--port", "8092"]
