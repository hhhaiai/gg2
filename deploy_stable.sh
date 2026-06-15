#!/bin/bash
set -e

echo "================================================"
echo "  grok2api 稳定性优化部署脚本"
echo "  目标: 2C2G 服务器 + 10 万账号稳定运行"
echo "================================================"
echo ""

# 检测 Docker Compose 命令
if docker compose version &> /dev/null; then
    COMPOSE_CMD="docker compose"
    echo "✅ 检测到新版 Docker Compose"
elif command -v docker-compose &> /dev/null; then
    COMPOSE_CMD="docker-compose"
    echo "✅ 检测到旧版 Docker Compose"
else
    echo "❌ 错误: 未找到 Docker Compose"
    echo "请先安装: sudo apt-get install docker-compose-plugin"
    exit 1
fi
echo "   使用命令: $COMPOSE_CMD"
echo ""

# 1. 停止容器
echo "📦 [1/6] 停止现有容器..."
$COMPOSE_CMD down

# 2. 压缩数据库（清理碎片）
if [ -f "data/accounts.db" ]; then
    echo "🗜️  [2/6] 压缩 SQLite 数据库..."
    DB_SIZE_BEFORE=$(du -h data/accounts.db | cut -f1)
    echo "   压缩前: $DB_SIZE_BEFORE"
    sqlite3 data/accounts.db "VACUUM;"
    DB_SIZE_AFTER=$(du -h data/accounts.db | cut -f1)
    echo "   压缩后: $DB_SIZE_AFTER ✅"
else
    echo "⚠️  [2/6] 未找到 data/accounts.db，跳过压缩"
fi

# 3. 清理旧日志（保留最近 3 天）
echo "🧹 [3/6] 清理旧日志文件..."
if [ -d "logs" ]; then
    OLD_LOGS=$(find logs/ -name "*.log" -mtime +3 2>/dev/null | wc -l)
    if [ "$OLD_LOGS" -gt 0 ]; then
        find logs/ -name "*.log" -mtime +3 -delete
        echo "   删除 $OLD_LOGS 个旧日志文件 ✅"
    else
        echo "   无需清理"
    fi
else
    mkdir -p logs
    echo "   创建 logs 目录"
fi

# 4. 验证配置文件
echo "🔍 [4/6] 验证配置文件..."
if [ -f "config.toml" ]; then
    if grep -q "enabled = false" config.toml; then
        echo "   ✅ config.toml 已配置 random 模式"
    else
        echo "   ⚠️  警告: config.toml 未设置 enabled = false"
        echo "   建议手动编辑 config.toml，设置 [account.refresh] enabled = false"
    fi
else
    echo "   ❌ 未找到 config.toml，请先创建配置文件"
    echo "   参考: SURVIVAL_GUIDE_2C2G.md"
    exit 1
fi

if grep -q "memory: 1.5G" docker-compose.yml; then
    echo "   ✅ docker-compose.yml 内存限制已设置为 1.5G"
else
    echo "   ⚠️  警告: docker-compose.yml 内存限制可能过低"
fi

# 5. 重新构建并启动
echo "🚀 [5/6] 重新构建并启动容器..."
$COMPOSE_CMD up -d --build

# 6. 等待服务启动
echo "⏳ [6/6] 等待服务启动..."
sleep 10

# 检查容器状态
if $COMPOSE_CMD ps | grep -q "grok2api.*Up"; then
    echo ""
    echo "✅ 部署成功！"
    echo ""
    echo "================================================"
    echo "📊 容器状态:"
    echo "================================================"
    docker stats --no-stream grok2api
    echo ""
    echo "================================================"
    echo "📝 最近日志:"
    echo "================================================"
    $COMPOSE_CMD logs --tail=20
    echo ""
    echo "================================================"
    echo "🎯 下一步:"
    echo "================================================"
    echo "1. 查看实时日志:"
    echo "   $COMPOSE_CMD logs -f"
    echo ""
    echo "2. 监控内存占用:"
    echo "   watch -n 5 'docker stats --no-stream grok2api'"
    echo ""
    echo "3. 测试 API:"
    echo "   curl http://localhost:8000/health"
    echo ""
    echo "4. 验证 random 模式:"
    echo "   $COMPOSE_CMD logs | grep 'selection strategy'"
    echo "   预期输出: selection strategy set to: random"
    echo ""
else
    echo ""
    echo "❌ 部署失败，请查看日志:"
    echo "   $COMPOSE_CMD logs"
    exit 1
fi
