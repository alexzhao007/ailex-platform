#!/bin/bash
# AiLex Platform — 备份脚本
set -e

BACKUP_DIR=./backups
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
mkdir -p $BACKUP_DIR

echo "📦 Backing up AiLex Platform..."

# Backup gateway data
if [ -d ./gateway/data ]; then
    tar czf "$BACKUP_DIR/gateway_$TIMESTAMP.tar.gz" ./gateway/data
fi

# Backup openclaw data
if [ -d ./data/openclaw ]; then
    tar czf "$BACKUP_DIR/openclaw_$TIMESTAMP.tar.gz" ./data/openclaw
fi

# Backup configs
tar czf "$BACKUP_DIR/configs_$TIMESTAMP.tar.gz" \
    .env \
    docker-compose.yml \
    gateway/config.yaml \
    openclaw/config.yaml

echo "✅ Backup saved to $BACKUP_DIR/"
echo "   Files:"
ls -lh $BACKUP_DIR/
