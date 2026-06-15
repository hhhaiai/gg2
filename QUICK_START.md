# 一键部署指南

## 🚀 快速开始

### 方式 1: 直接部署（推荐）

```bash
# 1. 进入项目目录
cd /path/to/grok2api

# 2. 一键启动（自动构建 + 后台运行）
docker compose up -d --build

# 3. 查看启动日志
docker compose logs -f
```

就这么简单！按 `Ctrl+C` 退出日志查看，容器会继续在后台运行。

---

## ✅ 验证部署

### 检查容器状态
```bash
docker compose ps
```
预期输出：
```
NAME       IMAGE     STATUS    PORTS
grok2api   ...       Up        0.0.0.0:8000->8000/tcp
```

### 检查内存占用（应该 <600 MB）
```bash
docker stats --no-stream grok2api
```

### 验证 API 可用
```bash
# 健康检查
curl http://localhost:8000/health

# 测试对话（需替换 YOUR_API_KEY）
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-2-1212",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": false
  }'
```

### 验证 random 模式已启用
```bash
docker compose logs | grep "selection strategy"
```
预期输出：`selection strategy set to: random`

---

## 📁 项目结构

```
grok2api/
├── docker-compose.yml      # ✅ 已配置（内存 1.5G）
├── config.toml            # ✅ 已配置（random 模式）
├── Dockerfile             # ✅ 已存在
├── data/
│   └── accounts.db        # 账号数据库
├── logs/                  # 日志目录
└── ...
```

**关键配置已就绪**：
- ✅ `config.toml` 挂载到容器
- ✅ 内存限制 1.5G（适配 2C2G 服务器）
- ✅ `enabled = false`（random 模式，内存占用 -65%）
- ✅ 日志级别 WARNING（可通过环境变量覆盖）

---

## 🎛️ 常用命令

### 启动服务
```bash
docker compose up -d          # 后台启动
docker compose up -d --build  # 重新构建后启动
```

### 停止服务
```bash
docker compose down           # 停止并删除容器
docker compose stop           # 仅停止容器（保留容器）
```

### 重启服务
```bash
docker compose restart        # 重启容器
```

### 查看日志
```bash
docker compose logs           # 查看所有日志
docker compose logs -f        # 实时查看日志
docker compose logs --tail=50 # 查看最近 50 行
```

### 进入容器
```bash
docker exec -it grok2api bash
```

### 查看资源占用
```bash
docker stats grok2api         # 实时监控
docker stats --no-stream grok2api  # 单次查看
```

---

## 🔧 自定义配置

### 修改端口（默认 8000）
```bash
# 方式 1: 环境变量
HOST_PORT=9000 docker compose up -d

# 方式 2: 创建 .env 文件
echo "HOST_PORT=9000" > .env
docker compose up -d
```

### 修改日志级别
```bash
# INFO（详细） / WARNING（警告） / ERROR（仅错误）
LOG_LEVEL=WARNING docker compose up -d
```

### 修改 worker 数量（不推荐多 worker）
```bash
SERVER_WORKERS=1 docker compose up -d  # 建议保持 1
```

---

## 📊 性能监控

### 持续监控内存（每 5 秒刷新）
```bash
watch -n 5 'docker stats --no-stream grok2api'
```

### 查看账号统计
```bash
docker exec grok2api sqlite3 /app/data/accounts.db \
  "SELECT status, COUNT(*) as count 
   FROM accounts 
   WHERE deleted_at IS NULL 
   GROUP BY status;"
```

### 查看最近错误日志
```bash
docker compose logs --tail=100 | grep -i error
```

---

## 🆘 常见问题

### Q1: 端口 8000 被占用
```bash
# 查看占用进程
lsof -i :8000

# 修改端口
HOST_PORT=9000 docker compose up -d
```

### Q2: 内存占用过高（>800 MB）
```bash
# 1. 检查是否启用了 random 模式
docker compose logs | grep "selection strategy"

# 2. 检查配置文件
cat config.toml | grep enabled

# 3. 重启容器释放内存
docker compose restart
```

### Q3: 容器启动失败
```bash
# 查看详细错误
docker compose logs

# 常见原因：
# - data/ 目录权限问题
# - config.toml 语法错误
# - 端口冲突
```

### Q4: API 返回 429 过多
```bash
# 检查冷却账号数量
docker exec grok2api sqlite3 /app/data/accounts.db \
  "SELECT COUNT(*) FROM accounts WHERE status = 'COOLING';"

# 如果超过 50%，说明账号质量差或请求频率过高
# 解决方案：
# 1. 增加账号数量
# 2. 降低请求频率
# 3. 调整冷却时间（config.toml 的 *_interval_sec）
```

---

## 🔄 更新部署

当你修改了配置文件或代码：

```bash
# 重新构建并重启
docker compose up -d --build

# 或分步执行
docker compose down
docker compose build
docker compose up -d
```

---

## 🗑️ 完全清理

如果需要完全重置：

```bash
# 停止并删除容器
docker compose down

# 删除镜像（可选）
docker rmi grok2api

# 清理数据（谨慎！会删除所有账号）
rm -rf data/accounts.db logs/*.log

# 重新启动
docker compose up -d --build
```

---

## 📦 生产环境部署

### 推荐配置（2C2G 服务器）

**已配置在 docker-compose.yml**:
```yaml
deploy:
  resources:
    limits:
      cpus: '1.5'      # 留 0.5 核给系统
      memory: 1.5G     # 留 0.5G 给系统
```

**系统要求**:
- CPU: 2 核 (建议 2 核以上)
- 内存: 2 GB (最低 1.5 GB)
- 磁盘: 500 MB + (账号数 × 2 KB)
- 网络: 100 Mbps 上行（建议）

**10 万账号规模**:
- 内存占用: ~400 MB (random 模式)
- CPU 占用: <1% (空闲) / 10-20% (50-100 RPS)
- 磁盘占用: ~300 MB (数据库 + 日志)

---

## ✅ 部署检查清单

部署完成后，逐项检查：

- [ ] `docker compose ps` 显示容器 Up
- [ ] `docker stats` 显示内存 <600 MB
- [ ] `curl http://localhost:8000/health` 返回 `{"status":"ok"}`
- [ ] `docker compose logs | grep "selection strategy"` 显示 `random`
- [ ] `docker compose logs | grep error` 无严重错误
- [ ] API 测试正常返回响应

---

## 📚 相关文档

- **SURVIVAL_GUIDE_2C2G.md**: 2C2G 服务器完整指南
- **PERFORMANCE_ANALYSIS.md**: 性能分析报告
- **DEPLOYMENT_CHECKLIST.md**: 详细部署检查清单
- **deploy_stable.sh**: 自动化部署脚本（包含数据库压缩等优化）

---

**最后更新**: 2026-06-15  
**测试环境**: macOS + Docker Desktop / Ubuntu 22.04  
**生产环境**: 2C2G 云服务器 + 24,718 账号
