# 文档清理方案

## 📊 当前状态
- **9 个 Markdown 文档**，共 3,185 行
- 内容重复度高（部署、配置说明出现 4-5 次）
- 用户需要读 3-4 个文档才能完整理解

## 🎯 清理建议

### 保留文件（3 个）

#### 1. README.md（主文档）
**保留理由**: 项目主入口，GitHub 首页展示  
**需要整合**:
- 添加 2C2G 部署说明（来自 SURVIVAL_GUIDE_2C2G.md）
- 添加一键部署指引（来自 README_DEPLOY.md）
- 添加导入优化说明（来自 IMPORT_OPTIMIZATION.md）

#### 2. deploy_stable.sh（部署脚本）
**保留理由**: 一键部署，功能完整  
**无需修改**

#### 3. config.toml（配置文件）
**保留理由**: random 模式关键配置  
**无需修改**

---

### 删除文件（6 个）

#### ❌ README_DEPLOY.md (300 行)
**删除理由**: 与 README.md 重复 80%  
**迁移内容**: 一键部署命令 → README.md

#### ❌ QUICK_START.md (306 行)
**删除理由**: 常用命令重复  
**迁移内容**: Docker 命令速查表 → README.md

#### ❌ SURVIVAL_GUIDE_2C2G.md (361 行)
**删除理由**: 核心配置已在 config.toml 体现  
**迁移内容**: random 模式说明 → README.md 新增章节

#### ❌ DEPLOYMENT_CHECKLIST.md (149 行)
**删除理由**: deploy_stable.sh 已自动完成检查  
**迁移内容**: 验证命令 → README.md

#### ❌ DOCKER_COMPOSE_COMPAT.md (174 行)
**删除理由**: deploy_stable.sh 已自动检测版本  
**迁移内容**: 简短说明 → README.md

#### ❌ IMPORT_OPTIMIZATION.md (217 行)
**删除理由**: 用户不需要知道实现细节  
**迁移内容**: 核心效果（30 秒导入 10 万账号）→ README.md

---

### 可选保留（2 个）

#### 🔸 CLAUDE.md (616 行)
**保留理由**: 深度技术分析，给贡献者/维护者看  
**建议**: 移动到 `docs/` 目录

#### 🔸 PERFORMANCE_ANALYSIS.md (436 行)
**保留理由**: 性能优化详细分析  
**建议**: 合并到 CLAUDE.md 或删除（信息已过时）

---

## ✅ 最终结果

### 精简后（3 个核心文件）
```
grok2api/
├── README.md                 # 整合后的完整文档（~800 行）
├── deploy_stable.sh          # 一键部署脚本
├── config.toml              # random 模式配置
├── docker-compose.yml       # Docker 配置
├── Dockerfile              # 镜像构建
└── docs/                   # 可选：深度文档
    └── CLAUDE.md           # 技术分析（给贡献者）
```

### 优势
- ✅ **一个 README 看懂全部**（无需跳转 4-5 个文档）
- ✅ **减少 2,400+ 行重复内容**
- ✅ **部署更简单**（README → deploy_stable.sh → 完成）
- ✅ **维护成本降低**（只需更新 1 个文档）

---

## 🚀 执行步骤

### 方案 A: 激进清理（推荐）
```bash
# 1. 删除重复文档
rm README_DEPLOY.md QUICK_START.md SURVIVAL_GUIDE_2C2G.md \
   DEPLOYMENT_CHECKLIST.md DOCKER_COMPOSE_COMPAT.md \
   IMPORT_OPTIMIZATION.md PERFORMANCE_ANALYSIS.md

# 2. 移动技术文档到 docs/
mkdir -p docs
mv CLAUDE.md docs/

# 3. 更新 README.md（整合关键信息）

# 4. 提交
git add -A
git commit -m "docs: 精简文档，整合到 README"
git push
```

### 方案 B: 保守清理
```bash
# 只删除明显重复的 3 个
rm README_DEPLOY.md QUICK_START.md DEPLOYMENT_CHECKLIST.md

# 其他的移到 docs/
mkdir -p docs
mv CLAUDE.md PERFORMANCE_ANALYSIS.md SURVIVAL_GUIDE_2C2G.md \
   DOCKER_COMPOSE_COMPAT.md IMPORT_OPTIMIZATION.md docs/

# 提交
git add -A
git commit -m "docs: 整理文档结构"
git push
```

---

## 📝 README.md 新增章节建议

```markdown
## 🚀 快速部署（2C2G 服务器）

### 一键启动
\`\`\`bash
git clone <your-repo>
cd grok2api
docker compose up -d --build  # 或 docker-compose（旧版）
\`\`\`

### random 模式（推荐：2C2G 服务器 + 10 万账号）
本项目已预配置 random 模式（`config.toml`）：
- ✅ 内存占用 -65%（10 万账号仅需 335 MB）
- ✅ CPU 占用 -90%（零后台任务）
- ✅ 导入 10 万账号 30 秒完成
- ✅ 零后台网络流量

详细说明见 `docs/SURVIVAL_GUIDE_2C2G.md`

### 完整部署（含数据库优化）
\`\`\`bash
./deploy_stable.sh
\`\`\`

自动完成：压缩数据库、清理日志、验证配置、重启服务。

---

## ⚙️ 配置说明

### 关键配置：config.toml
\`\`\`toml
[account.refresh]
enabled = false  # random 模式（2C2G 推荐）
# enabled = true # quota 模式（4C8G+ 服务器）
\`\`\`

### Docker Compose 版本
- 新版: `docker compose` (空格)
- 旧版: `docker-compose` (连字符)
脚本自动检测版本，无需手动修改。
```

---

**你的选择？**
- A) 激进清理（删除 6 个，只保留 README + 脚本 + 配置）
- B) 保守清理（删除 3 个重复的，其他移到 docs/）
- C) 自定义（告诉我保留哪些）
