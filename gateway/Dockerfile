FROM python:3.11-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir \
    fastapi uvicorn httpx pyyaml pydantic python-dotenv

# Copy code
COPY gateway.py .
COPY config.yaml .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Create data dir
RUN mkdir -p /app/data

EXPOSE 8080

ENTRYPOINT ["./entrypoint.sh"]
