#!/bin/bash
# 远程诊断脚本 - 在服务器上执行

echo "========================================="
echo "  grok2api 性能诊断"
echo "========================================="
echo ""

# 1. 检查容器状态
echo "📊 [1/6] 容器资源占用:"
docker stats --no-stream grok2api
echo ""

# 2. 检查配置模式
echo "🔍 [2/6] 检查配置模式:"
if docker exec grok2api test -f /app/config.toml; then
    echo "   ✅ config.toml 已挂载"
    docker exec grok2api grep -A 3 "account.refresh" /app/config.toml || echo "   ⚠️  配置格式异常"
else
    echo "   ❌ config.toml 未挂载！正在使用默认配置（quota 模式）"
fi
echo ""

# 3. 检查运行模式
echo "🎯 [3/6] 实际运行模式:"
docker-compose logs | grep -i "selection strategy" | tail -1
echo ""

# 4. 检查账号统计
echo "📈 [4/6] 账号状态统计:"
docker exec grok2api sqlite3 /app/data/accounts.db \
  "SELECT status, COUNT(*) as count FROM accounts WHERE deleted_at IS NULL GROUP BY status;" \
  2>/dev/null || echo "   ⚠️  无法读取数据库"
echo ""

# 5. 检查导入/刷新日志
echo "📝 [5/6] 最近导入/刷新日志:"
docker-compose logs --tail=100 | grep -E "import|refresh|chunk" | tail -10
echo ""

# 6. 诊断结论
echo "========================================="
echo "💡 诊断结论:"
echo "========================================="

# 检查是否在 quota 模式
if docker-compose logs | grep -q "selection strategy set to: random"; then
    echo "✅ 当前使用 random 模式（正确）"
    echo ""
    echo "但性能问题可能来自："
    echo "  1. 首次启动时的 bootstrap 加载"
    echo "  2. 大量并发 API 请求"
    echo "  3. 内存不足（1.2G 已超限）"
else
    echo "❌ 当前使用 quota 模式（问题根源！）"
    echo ""
    echo "正在后台测试所有账号配额，导致："
    echo "  1. CPU 持续高占用（20%-78%）"
    echo "  2. 网络流量暴增"
    echo "  3. 验证速度慢（每个账号 1-3 秒）"
    echo ""
    echo "预计完成时间："
    echo "  - 37,703 个账号已完成"
    echo "  - 如果总数 10 万，还需 30-40 分钟"
    echo ""
    echo "========================================="
    echo "🔧 修复方案:"
    echo "========================================="
    echo ""
    echo "立即停止并切换到 random 模式："
    echo "  1. cd ~/gg2"
    echo "  2. docker-compose down"
    echo "  3. 确认 config.toml 存在："
    echo "     ls -la config.toml"
    echo "  4. 确认 docker-compose.yml 挂载了 config.toml："
    echo "     grep 'config.toml' docker-compose.yml"
    echo "  5. 重新启动："
    echo "     docker-compose up -d"
    echo ""
fi
