#!/bin/bash
# AiLex Platform — One-Click Deploy
set -e

echo "╔══════════════════════════════════════╗"
echo "║    AiLex Platform — 一键部署          ║"
echo "║  OpenClaw + 万量引擎 · 开源一体化    ║"
echo "╚══════════════════════════════════════╝"
echo ""

# Check prerequisites
for cmd in docker docker-compose; do
    if ! command -v $cmd &> /dev/null; then
        echo "❌ $cmd not found. Please install Docker first."
        exit 1
    fi
done

# Check .env
if [ ! -f .env ]; then
    echo "📝 Creating .env from .env.example..."
    cp .env.example .env
    echo "⚠  Please edit .env to set your MMI_API_KEY"
    echo "   Get your key at https://millionengine.com"
    exit 1
fi

# Load .env
source .env

# Validate API key
if [ -z "$MMI_API_KEY" ] || [ "$MMI_API_KEY" = "your-api-key-here" ]; then
    echo "❌ MMI_API_KEY is not set in .env"
    exit 1
fi

echo "✅ Prerequisites check passed"
echo ""

# Build and start
echo "🚀 Building and starting services..."
docker compose build --quiet
docker compose up -d

echo ""
echo "✅ AiLex Platform is running!"
echo ""
echo "  📊 Management UI:  http://localhost:${UI_PORT:-3001}"
echo "  🔌 OpenAI API:     http://localhost:${GATEWAY_PORT:-8080}/v1"
echo "  💚 OpenClaw:       http://localhost:${OPENCLAW_PORT:-3000}"
echo ""
echo "  Test the API:"
echo "    curl http://localhost:${GATEWAY_PORT:-8080}/v1/chat/completions \\"
echo "      -H 'Content-Type: application/json' \\"
echo "      -d '{\"model\":\"gpt-4o\",\"messages\":[{\"role\":\"user\",\"content\":\"Hi\"}]}'"
echo ""
echo "  View logs: docker compose logs -f"
echo "  Stop:      docker compose down"
