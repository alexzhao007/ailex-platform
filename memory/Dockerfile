FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn pydantic numpy

COPY memory_service.py .
RUN mkdir -p /app/data

EXPOSE 8090

CMD ["uvicorn", "memory_service:app", "--host", "0.0.0.0", "--port", "8090"]
