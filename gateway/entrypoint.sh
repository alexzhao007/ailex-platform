#!/bin/bash
# Gateway entrypoint — run config check then start server

set -e

echo "=== AiLex Gateway ==="
echo "Checking configuration..."

if [ -z "$MMI_API_KEY" ] || [ "$MMI_API_KEY" = "your-api-key-here" ]; then
    echo "⚠ WARNING: MMI_API_KEY not set. Set it in .env or as environment variable."
    echo "  Get your key at https://millionengine.com"
fi

echo "Default model: ${DEFAULT_MODEL:-gpt-4o}"
echo "Gateway URL: ${MMI_BASE_URL:-https://millionengine.com/v1}"

# Start the FastAPI server
exec uvicorn gateway:app --host 0.0.0.0 --port 8080 --log-level ${LOG_LEVEL:-info}
