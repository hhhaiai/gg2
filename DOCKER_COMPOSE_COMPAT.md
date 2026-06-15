# Docker Compose 版本兼容说明

## 两种命令格式

Docker Compose 有两种命令格式：

### 新版（Docker Compose V2，2022 年后）
```bash
docker compose up -d        # 空格
```

### 旧版（Docker Compose V1）
```bash
docker-compose up -d        # 连字符
```

---

## 🔍 检测你的版本

```bash
# 检测新版
docker compose version

# 检测旧版
docker-compose --version
```

---

## 📚 文档中的命令适配

### 本项目所有文档中的命令

所有文档（README_DEPLOY.md、QUICK_START.md 等）中的命令都使用**新版语法**：
```bash
docker compose up -d
```

### 如果你使用旧版，请替换为

```bash
docker-compose up -d
```

---

## 🛠️ 快速替换规则

| 文档中的命令 | 旧版对应命令 |
|-------------|-------------|
| `docker compose up -d` | `docker-compose up -d` |
| `docker compose down` | `docker-compose down` |
| `docker compose restart` | `docker-compose restart` |
| `docker compose logs -f` | `docker-compose logs -f` |
| `docker compose ps` | `docker-compose ps` |
| `docker compose build` | `docker-compose build` |

**唯一区别**: `compose` → `compose`（空格改为连字符）

---

## 🚀 一键部署（适配旧版）

### 在服务器上执行

```bash
# 1. 进入项目目录
cd ~/gg2

# 2. 停止旧容器（如果有）
docker-compose down

# 3. 拉取最新代码
git pull origin main

# 4. 启动（旧版语法）
docker-compose up -d --build

# 5. 查看日志
docker-compose logs -f
```

---

## 📊 验证部署

```bash
# 检查容器状态
docker-compose ps

# 检查内存占用
docker stats --no-stream grok2api

# 验证 random 模式
docker-compose logs | grep "selection strategy"

# 测试 API
curl http://localhost:8000/health
```

---

## 🔄 升级到新版（可选）

如果想升级到新版 Docker Compose：

### Ubuntu/Debian
```bash
# 安装新版 Docker Compose（V2）
sudo apt-get update
sudo apt-get install docker-compose-plugin

# 验证
docker compose version
```

### 或使用官方安装脚本
```bash
# 下载最新版本
DOCKER_CONFIG=${DOCKER_CONFIG:-$HOME/.docker}
mkdir -p $DOCKER_CONFIG/cli-plugins
curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
  -o $DOCKER_CONFIG/cli-plugins/docker-compose
chmod +x $DOCKER_CONFIG/cli-plugins/docker-compose

# 验证
docker compose version
```

---

## ⚠️ 注意事项

1. **两个版本可以共存**，但建议只用一个
2. **旧版将在 2024 年停止维护**，建议升级到新版
3. **功能完全相同**，只是命令格式不同
4. 本项目的 `docker-compose.yml` 文件**两个版本通用**

---

## 🆘 常见错误

### 错误 1: `unknown shorthand flag: 'd' in -d`
**原因**: 使用了新版语法但系统只有旧版  
**解决**: 改用 `docker-compose`（连字符）

### 错误 2: `docker-compose: command not found`
**原因**: 未安装 Docker Compose  
**解决**: 
```bash
# Ubuntu/Debian
sudo apt-get install docker-compose

# 或安装新版
sudo apt-get install docker-compose-plugin
```

### 错误 3: `permission denied`
**原因**: 没有 Docker 权限  
**解决**:
```bash
# 方案 1: 加 sudo
sudo docker-compose up -d

# 方案 2: 加入 docker 组（推荐）
sudo usermod -aG docker $USER
# 重新登录生效
```

---

**最后更新**: 2026-06-15  
**兼容性**: Docker Compose V1 & V2 通用
