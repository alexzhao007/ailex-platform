FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn pydantic httpx pyyaml

COPY orch_service.py .

RUN mkdir -p /app/data

EXPOSE 8095

CMD ["uvicorn", "orch_service:app", "--host", "0.0.0.0", "--port", "8095"]
