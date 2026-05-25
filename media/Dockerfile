FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn pydantic httpx python-multipart

COPY media_service.py .
RUN mkdir -p /app/data/media

EXPOSE 8091

CMD ["uvicorn", "media_service:app", "--host", "0.0.0.0", "--port", "8091"]
