# AiLex Platform — 部署指南

## 生产部署

### 前置要求

- Docker 24+ & Docker Compose 2+
- 2GB+ 内存
- 10GB+ 磁盘空间
- 域名（可选，配 HTTPS）

### 步骤

```bash
# 1. 克隆
git clone https://github.com/your/ailex-platform.git
cd ailex-platform

# 2. 配置
cp .env.example .env
vim .env  # 填入 MMI_API_KEY

# 3. 部署
docker compose up -d
```

### Nginx + HTTPS（可选）

```nginx
server {
    listen 443 ssl;
    server_name your-domain.com;
    
    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;
    
    location / {
        proxy_pass http://localhost:3001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    
    location /api/ {
        proxy_pass http://localhost:8080/;
        proxy_set_header Host $host;
    }
}
```

## 升级

```bash
git pull
docker compose pull
docker compose up -d --force-recreate
```

## 扩展

### 多节点集群

将 `docker-compose.yml` 中的 `gateway` 服务扩展到多实例：

```bash
docker compose up -d --scale gateway=3
```

### 持久化

所有数据存储在 `./data/` 和 Docker volumes 中。定期运行 `./scripts/backup.sh`。

## 故障排查

**Gateway 启动失败**
```bash
docker compose logs gateway
```

**模型调用超时**
- 检查 MMI_API_KEY 是否有效
- 检查网络是否能访问 millionengine.com

**OpenClaw 无法连接 Gateway**
- 确保 gateway 服务健康：`curl http://localhost:8080/health`
- 检查 openclaw/config.yaml 中的 base_url
