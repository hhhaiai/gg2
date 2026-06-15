#!/bin/bash
# 服务器部署脚本 - 在服务器上执行

set -e

echo "================================================"
echo "  grok2api 服务器部署"
echo "  拉取最新代码并重启服务"
echo "================================================"
echo ""

# 进入项目目录
cd ~/gg2

echo "📥 [1/5] 拉取最新代码..."
git pull origin main

echo ""
echo "📦 [2/5] 检查 Docker Compose..."
if docker compose version &> /dev/null; then
    COMPOSE_CMD="docker compose"
    echo "   ✅ 使用新版: docker compose"
elif command -v docker-compose &> /dev/null; then
    COMPOSE_CMD="docker-compose"
    echo "   ✅ 使用旧版: docker-compose"
else
    echo "   ❌ 错误: 未找到 Docker Compose"
    exit 1
fi

echo ""
echo "🛑 [3/5] 停止现有容器..."
$COMPOSE_CMD down

echo ""
echo "🚀 [4/5] 重新构建并启动..."
$COMPOSE_CMD up -d --build

echo ""
echo "⏳ [5/5] 等待服务启动..."
sleep 10

echo ""
echo "================================================"
echo "✅ 部署完成！"
echo "================================================"
echo ""

# 检查状态
if $COMPOSE_CMD ps | grep -q "grok2api.*Up"; then
    echo "📊 容器状态:"
    docker stats --no-stream grok2api
    echo ""

    echo "📝 最近日志:"
    $COMPOSE_CMD logs --tail=30
    echo ""

    echo "================================================"
    echo "🎯 验证清单:"
    echo "================================================"
    echo ""
    echo "1. 检查 random 模式:"
    echo "   $COMPOSE_CMD logs | grep 'selection strategy'"
    echo "   预期: selection strategy set to: random"
    echo ""
    echo "2. 测试 API:"
    echo "   curl http://localhost:8000/health"
    echo ""
    echo "3. 查看实时日志:"
    echo "   $COMPOSE_CMD logs -f"
    echo ""
    echo "4. 监控内存:"
    echo "   watch -n 5 'docker stats --no-stream grok2api'"
    echo ""
else
    echo "❌ 错误: 容器启动失败"
    echo ""
    echo "查看日志:"
    $COMPOSE_CMD logs --tail=50
    exit 1
fi
