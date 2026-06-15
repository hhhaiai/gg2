# 过期账号判定逻辑分析

## 🔍 当前判定规则

### 什么时候账号会被标记为 EXPIRED？

**触发条件**（三者同时满足）：
1. HTTP 状态码是 `400`、`401` 或 `403`
2. 响应体包含特定关键词：
   - `"invalid_token"`
   - `"blocked-user"`
   - `"session not found"`
   - `"account suspended"`
   - `"token revoked"`
   - `"token expired"`

**代码位置**：
```python
# app/dataplane/reverse/protocol/xai_usage.py:217-223
def is_invalid_credentials_error(exc: BaseException) -> bool:
    if not isinstance(exc, UpstreamError):
        return False
    if exc.status not in (400, 401, 403):
        return False
    return is_invalid_credentials_body(str(exc.details.get("body", "") or ""))

# xai_usage.py:202-214
def is_invalid_credentials_body(text: str) -> bool:
    text = text.lower()
    return (
        "invalid_token" in text
        or "blocked-user" in text
        or "session not found" in text
        or "account suspended" in text
        or "token revoked" in text
        or "token expired" in text
    )
```

---

## ⚠️ 问题：误判风险

### 场景 1: 临时 403（并非真正过期）
**可能原因**：
- Cloudflare 挑战（CF challenge）
- 临时 IP 封禁
- 上游服务暂时不可用

**当前行为**：
- 如果响应体包含 `"blocked-user"` → 立即标记 EXPIRED
- **即使账号本身有效**，也会被永久排除

### 场景 2: 401 但账号仍可用
**可能原因**：
- Cookie 过期（可以重新登录）
- 会话超时（可以刷新）

**当前行为**：
- 如果响应体包含 `"session not found"` → 立即标记 EXPIRED
- **但账号本身可能只需要刷新 token**

### 场景 3: 网络超时误判
**可能原因**：
- 家用网络不稳定
- 代理连接超时
- 上游服务抖动

**当前行为**：
- 如果恰好返回 403 + `"blocked-user"` → 错误标记

---

## ✅ 建议优化方案

### 方案 1: 三次确认机制（推荐）

```python
# 不要立即标记 EXPIRED，而是累计 strikes
def is_invalid_credentials_error(exc: BaseException) -> tuple[bool, bool]:
    """
    返回: (is_invalid, should_confirm)
    - is_invalid: 确定是无效凭证
    - should_confirm: 需要多次确认
    """
    if not isinstance(exc, UpstreamError):
        return False, False
    
    if exc.status not in (400, 401, 403):
        return False, False
    
    body = str(exc.details.get("body", "") or "").lower()
    
    # 明确的永久失效标记（一次确认即可）
    permanent_markers = [
        "token revoked",      # token 已被撤销
        "account suspended",  # 账号被封禁
    ]
    if any(m in body for m in permanent_markers):
        return True, False  # 确定无效，无需多次确认
    
    # 可能的临时问题（需要三次确认）
    temporary_markers = [
        "invalid_token",
        "blocked-user",
        "session not found",
        "token expired",
    ]
    if any(m in body for m in temporary_markers):
        return True, True  # 可能无效，需要确认
    
    return False, False

# 在 state_machine.py 中记录 strikes
if is_invalid and should_confirm:
    strikes = record.ext.get("invalid_strikes", 0) + 1
    if strikes >= 3:
        # 三次都失败，确认是真过期
        status = AccountStatus.EXPIRED
    else:
        # 只是累计 strikes，继续使用
        ext["invalid_strikes"] = strikes
        ext["last_invalid_at"] = now_ms()
else:
    # 成功了，清除 strikes
    ext.pop("invalid_strikes", None)
```

**优势**：
- 避免临时问题导致误判
- 真正的过期账号会在 3 次内被识别
- 临时抖动的账号会在成功后清除 strikes

---

### 方案 2: 延迟验证（保守）

```python
# 标记为 EXPIRED 后，24 小时内仍尝试使用
def is_selectable(record, mode_id, *, now):
    if record.is_deleted():
        return False
    
    status = derive_status(record, now=now)
    
    # EXPIRED 账号在 24 小时内仍可选（给一次复活机会）
    if status == AccountStatus.EXPIRED:
        expired_at = record.ext.get("expired_at", 0)
        grace_period = 24 * 3600 * 1000  # 24 小时
        if now - expired_at < grace_period:
            return True  # 宽限期内仍可用
        return False
    
    if status != AccountStatus.ACTIVE:
        return False
    
    # ... 其他判断
```

**优势**：
- 简单改动
- 给"假过期"账号 24 小时复活机会
- 真正过期的账号会在 24 小时内再次失败，最终被排除

---

### 方案 3: 智能重试（random 模式最适合）

```python
# random 模式下，EXPIRED 账号也参与选号，但优先级最低
def _random_select(table, pool_id, *, exclude_idxs, now_s):
    # 先选 ACTIVE 账号
    active_candidates = {idx for idx in candidates if status[idx] == ACTIVE}
    if active_candidates:
        return random.choice(list(active_candidates))
    
    # ACTIVE 都用完了，尝试 EXPIRED 账号（可能只是临时问题）
    expired_candidates = {idx for idx in candidates if status[idx] == EXPIRED}
    if expired_candidates:
        logger.warning("all ACTIVE exhausted, trying EXPIRED accounts")
        return random.choice(list(expired_candidates))
    
    return None
```

**优势**：
- 零配置，完全自动
- 正常情况下不用 EXPIRED 账号
- 极端情况下（所有账号都 429）会尝试 EXPIRED 账号
- 如果 EXPIRED 账号成功，自动恢复为 ACTIVE

---

## 🎯 推荐实施方案

### 对于你的场景（10 万账号 + random 模式）

**推荐：方案 3（智能重试）**

理由：
1. ✅ **零配置**：无需修改现有逻辑
2. ✅ **容错能力强**：假过期账号会自动复活
3. ✅ **不影响正常流量**：优先用 ACTIVE 账号
4. ✅ **极端情况兜底**：所有账号 429 时仍有账号可用

**代码改动量**：~20 行（仅修改 selector.py）

---

## 🔧 快速验证"假过期"

### 检查过期账号
```bash
# 查看过期账号的过期原因
docker exec grok2api sqlite3 /app/data/accounts.db \
  "SELECT token, json_extract(ext, '$.expired_reason'), json_extract(ext, '$.expired_at')
   FROM accounts 
   WHERE status = 'EXPIRED' 
   LIMIT 10;"
```

### 手动恢复一个账号测试
```bash
# 1. 找一个 EXPIRED 账号
EXPIRED_TOKEN="xxxxx"

# 2. 改为 ACTIVE
docker exec grok2api sqlite3 /app/data/accounts.db \
  "UPDATE accounts SET status = 'ACTIVE' WHERE token = '$EXPIRED_TOKEN';"

# 3. 测试是否真的不能用
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{"model": "grok-2-1212", "messages": [{"role": "user", "content": "test"}]}'

# 4. 观察日志
docker-compose logs -f | grep "$EXPIRED_TOKEN"
```

如果手动恢复后能正常使用 → 说明是误判，需要优化判定逻辑

---

**你想先验证一下有多少"假过期"账号吗？还是直接实施方案 3？**
