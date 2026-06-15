# 部署检查清单

在执行部署脚本前，请确认以下事项：

## ✅ 部署前检查

- [ ] 已备份 `data/accounts.db`（重要！）
- [ ] 已创建 `config.toml`（设置 `enabled = false`）
- [ ] 已更新 `docker-compose.yml`（内存限制 1.5G）
- [ ] 服务器至少有 2C2G 配置
- [ ] 确认当前没有正在进行的 API 调用

## 🚀 快速部署

```bash
# 一键部署（执行所有优化）
./deploy_stable.sh
```

## 📊 部署后验证

### 1. 检查内存占用（应该 <600 MB）
```bash
docker stats --no-stream grok2api
```
**预期**: MEM USAGE < 600 MB

### 2. 验证 random 模式已启用
```bash
docker compose logs | grep "selection strategy"
```
**预期输出**: `selection strategy set to: random`

### 3. 确认无后台刷新任务
```bash
docker compose logs | grep "scheduled refresh"
```
**预期**: 仅启动时有一条消息，之后无输出

### 4. 测试 API 可用性
```bash
curl http://localhost:8000/health
```
**预期**: `{"status":"ok"}`

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-2-1212",
    "messages": [{"role": "user", "content": "测试"}],
    "stream": false
  }'
```
**预期**: 正常返回 JSON 响应

## 🔍 监控指标

### 持续监控内存（每 5 秒刷新）
```bash
watch -n 5 'docker stats --no-stream grok2api'
```

### 查看实时日志
```bash
docker compose logs -f --tail=50
```

### 检查账号使用情况（进入容器）
```bash
docker exec -it grok2api sqlite3 /app/data/accounts.db \
  "SELECT status, COUNT(*) FROM accounts WHERE deleted_at IS NULL GROUP BY status;"
```

## ⚠️ 故障排查

### 问题 1: 容器启动后立即退出
```bash
# 查看详细日志
docker compose logs

# 常见原因:
# - config.toml 语法错误
# - 端口 8000 被占用
# - 数据库文件损坏
```

### 问题 2: 内存持续增长超过 800 MB
```bash
# 重启容器释放内存
docker compose restart

# 如果重启后仍增长，检查:
# 1. 是否有大量并发请求
# 2. 是否误配置了 enabled = true
# 3. 日志文件是否过大
```

### 问题 3: API 返回 429 错误过多
```bash
# 检查冷却账号数量
docker exec -it grok2api sqlite3 /app/data/accounts.db \
  "SELECT COUNT(*) FROM accounts WHERE status = 'COOLING';"

# 如果 COOLING 账号过多（>50%），说明:
# 1. 账号质量差
# 2. 请求速率超过账号池容量
# 3. 需要增加账号数量
```

## 🎯 性能基准

| 指标 | quota 模式 | random 模式 | 改善 |
|------|-----------|-------------|------|
| 内存占用 (10 万账号) | 975 MB | 335 MB | **-65%** |
| CPU 占用 (空闲) | 5-10% | <1% | **-90%** |
| 启动时间 | 30-60s | 2-5s | **-83%** |
| 后台网络流量 | 持续 | 零 | **-100%** |

## 📚 相关文档

- **SURVIVAL_GUIDE_2C2G.md**: 2C2G 服务器配置完整指南
- **PERFORMANCE_ANALYSIS.md**: 性能分析报告
- **CLAUDE.md**: 项目深度审查（上游对比）

## 🆘 紧急回滚

如果优化后出现问题，回滚到原配置：

```bash
# 1. 停止容器
docker compose down

# 2. 恢复配置
rm config.toml  # 使用默认配置

# 3. 恢复 docker-compose.yml
git checkout docker-compose.yml

# 4. 重启
docker compose up -d
```

---

**最后更新**: 2026-06-15  
**适用版本**: grok2api latest  
**测试环境**: 2C2G + 24,718 账号
