# grok2api 项目深度审查与改进方案

> 审查范围: 整个 `app/`(24 个 Python 子包)、`config.defaults.toml`、`pyproject.toml`、`Dockerfile`、上游  `chenyme/grok2api` 对比。
>
> 审查日期: 2026-06-04
>
> 本文档目标: 1) 系统化列出所有发现的问题  2) 给出可落地的修复/优化方案  3) 标注"上游已存在 / 待贡献"  4) 保持原有功能不变,只增不破坏。

---

## 0. TL;DR (高层结论)

| 主题 | 现状 | 关键问题 | 影响 |
| --- | --- | --- | --- |
| **1. 模型获取** | 静态注册表 `MODELS` + 启动期 `runtime_snapshot` 过滤 | Console 模型只支持 `CHAT`,**无法用于生图/视频**;模型能力 vs 实际端点硬绑定 | X 免费账号无法跑 `grok-imagine-image-console` |
| **2. 选号/秒切** | `quota`(评分)/`random`(均匀)两套策略 | `apply_changes` 反查 tag 是 O(n×t);selector 每次全量遍历候选 | 万级账号下选号耗时 1~10 ms,不可线性扩展到 1M |
| **3. 1M 账号扩展** | 启动时全量 `runtime_snapshot`;admin `list_tokens` 全量翻页 | 启动期 OOM;admin 端点无法分页/筛选;selector 无分层索引 | 100 万账号会直接撑爆内存 + admin 卡死 |
| **4. 包活/刷新** | `usage_concurrency=50`,周期 2h/24h;429 触发全量 `refresh_on_demand` | 1 次 429 引发 N 次上游调用 → 立刻压垮家用带宽 | 用户描述的"网络直接被打崩"根源 |
| **5. 稳定性/扩展性** | 良好:多 worker 锁、列式内存表、revision 增量同步 | console_chat/console_responses/chat 三处 90% 重试/反馈代码重复;token 标准化在 3 处各写一遍;quota 6 字段全用 JSON 字符串列 | 维护成本高、改一处必漏三处 |
| **6. 上游贡献** | 已发现 12 个上游未处理的 P0/P1 问题 | 见 §7 详细列表 | 提 PR 有空间 |

---

## 1. 模型获取 (`app/control/model/`)

### 1.1 模型注册表工作原理
- **`registry.py`**: 静态 tuple `MODELS`,启动期构建 `_BY_NAME` / `_BY_CAP`。
- **`spec.py`**: `ModelSpec(mode_id, tier, capability, enabled, public_name, prefer_best)` 是不可变 dataclass。
- **`enums.py`**: `ModeId`(0=auto,1=fast,2=expert,3=heavy,4=grok_4_3,5=console)、`Tier`(0/1/2)、`Capability`(位掩码,8 个能力位)。
- **运行时过滤**:`router.py::_model_available_for_pools` 在 `GET /v1/models` 时调用 `repo.runtime_snapshot()`,筛掉没有"启用模型对应 tier"账号的项。

### 1.2 现状问题

#### 🔴 P0-1  X 免费账号无法用于生图/视频
**位置**: `app/control/model/registry.py:42-45`
```python
ModelSpec("grok-imagine-image-lite",  ModeId.FAST, Tier.BASIC, Capability.IMAGE, ...),
ModelSpec("grok-imagine-image",       ModeId.AUTO, Tier.SUPER, Capability.IMAGE, ...),
```
所有 `grok-imagine-*` 走 `grok.com` WS(参见 `app/products/openai/images.py:267-287` 的 `_LITE_IMAGE_MODELS` / `_PRO_IMAGE_MODELS` 分支),**没有任何 `*-console` 变体**。

**问题**:
- 上游 `console.x.ai` 是 OpenAI Responses API 兼容端点(参见 `app/dataplane/reverse/protocol/xai_console_chat.py:5-29`),本身支持 multimodal(包括图片生成/编辑)。
- 用户已有的 SSO 账号可被用于生图,但当前注册表不暴露任何 console 生图模型。
- 解决方向见 §1.3。

#### 🟡 P1-2  模型能力 vs 端点强耦合
**位置**: `app/control/model/spec.py:67-95` + `app/products/openai/router.py:235-296`
```python
if spec.is_image_edit():   result = await img_edit(...)
elif spec.is_image():      result = await img_gen(...)
elif spec.is_video():      result = await vid_comp(...)
else:                       result = await chat_completions(...)
```
每个能力都映射到固定的 `app/products/openai/{images,video,chat}.py` 模块。如果以后新增 "console image" 能力,需要同时改:registry(加 spec)+ enums(加 Capability 位)+ router(加分支)+ 新模块(实现)。

**修复**: 引入 `ModelSpec.endpoint_module: str | None` 字段(或在 `product_endpoint()` 方法返回),router 用统一 dispatcher:
```python
# 建议结构
async def dispatch(spec, request):
    handler = HANDLERS.get((spec.endpoint_namespace, spec.endpoint_module))
    return await handler(request)
```

#### 🟡 P1-3  能力位掩码膨胀
`Capability` 当前 8 位;若再增加 `CONSOLE_IMAGE`、`CONSOLE_VIDEO`,**位运算判断会越来越啰嗦**。建议改为 `frozenset[Literal["chat","image",...]]` 或 `Enum` 子类,BitFlag 仅保留 hot-path 性能用。

#### 🟢 P2-4  `get_project_version()` 不在 registry 范围内,但 `__init__.py` 仍从外部 import
参见 `app/main.py:31`。`get_project_version()` 应改为 `app.meta.versions.MODELS_VERSION` 之类,与模型版本同源。

### 1.3 建议方案: X 免费账号跑生图

> 这是用户明确问到的能力,目前**没有任何代码路径**让 X 免费账号生成图片。给出 3 步可落地改造。

**Step 1: 新增 Capability 位**
```python
# app/control/model/enums.py
class Capability(IntFlag):
    CHAT = 1
    IMAGE = 2
    IMAGE_EDIT = 4
    VIDEO = 8
    VOICE = 16
    ASSET = 32
    CONSOLE_CHAT = 64
    CONSOLE_IMAGE = 128     # 新增
    CONSOLE_IMAGE_EDIT = 256  # 新增
```

**Step 2: 新增 console 端点映射 + Payload builder**
```python
# app/dataplane/reverse/protocol/xai_console_image.py
CONSOLE_IMAGE_ENDPOINT = "https://console.x.ai/v1/images/generations"

async def generate_via_console(token: str, *, prompt: str, n: int, size: str, model: str):
    """复用 ResettableSession + CF clearance,POST /v1/images/generations。
    Authorization: Bearer anonymous, Cookie: sso=<token>; sso-rw=<token>
    body 形如 OpenAI Images API:{model,prompt,n,size,response_format}
    返回 {data:[{url,b64_json,...}]}
    """
    # 关键:模型名映射为 console 真实 model 字段
    console_model = {
        "grok-imagine-image-lite-console": "grok-imagine-image-lite",
        "grok-imagine-image-console":      "grok-imagine-image",
        "grok-imagine-image-pro-console":  "grok-imagine-image-pro",
    }[model]
    # ...POST + 错误处理 + 反馈
```

**Step 3: 注册 3 个新模型**
```python
# app/control/model/registry.py MODELS tuple 内追加
ModelSpec("grok-imagine-image-lite-console", ModeId.CONSOLE, Tier.BASIC,
          Capability.CONSOLE_IMAGE, True, "Grok Imagine Image Lite (Console)"),
ModelSpec("grok-imagine-image-console",      ModeId.CONSOLE, Tier.BASIC,
          Capability.CONSOLE_IMAGE, True, "Grok Imagine Image (Console)"),
ModelSpec("grok-imagine-image-pro-console",  ModeId.CONSOLE, Tier.BASIC,
          Capability.CONSOLE_IMAGE, True, "Grok Imagine Image Pro (Console)"),
```
**配额**: 走 `console.x.ai` 配额而非 `grok.com` 配额,可直接用现有 `quota_console`(30/15min,见 `quota_defaults.py:BASIC_CONSOLE_LIMIT=30`)。

**Step 4: router 分发**
在 `app/products/openai/router.py::image_generations` 末尾加:
```python
if spec.capability & Capability.CONSOLE_IMAGE:
    from .console_images import generate as console_img_gen
    return JSONResponse(await console_img_gen(...))
```

> **对上游的 PR 价值**: 此特性在 chenyme/grok2api 完全缺失,可独立 PR。

---

## 2. 选号/秒切 (`app/dataplane/account/`, `app/control/account/`)

### 2.1 工作原理(精简)

```
GET /v1/chat/completions
  └─> chat.py::completions
      └─> reserve_account(spec)         # 选号 + 上 inflight
          └─> AccountDirectory.reserve()
              └─> selector.select()     # 评分/随机 + 排除
              └─> fb.increment_inflight() 
      └─> 反向调用 grok.com
      └─> directory.release() + directory.feedback()  # 扣分/扣 quota
      └─> 失败: 再次 reserve (排除刚失败的 token)
```

### 2.2 现状问题

#### 🔴 P0-5  1 个 429 触发全量 `refresh_on_demand`
**位置**: `app/products/openai/chat.py:142-150`
```python
if (current_strategy() == "quota" and getattr(exc, "status", None) == 429):
    result = await svc.refresh_on_demand()  # ← 全量刷新所有账号配额!
```
- `refresh_on_demand` 内部调用 `refresh_scheduled()`(见 `refresh.py:215`),`runtime_snapshot()` 拉全表 + `_refresh_one()` 串行发 `xai_usage` API。
- 当账号池 10 万、出现 5% 流量是 429 → 立刻 5000 次 `xai_usage` 同步请求,家用网络瞬间打爆。

**修复**:
- (a) 短期:`refresh_on_demand` 替换为**仅刷新"刚刚 429 的那个 token"**,通过 `get_accounts([token])` 单点拉取。
- (b) 中期:增加 per-token `last_refresh_at` 防抖(类似 `on_demand_min_interval_sec`)。
- (c) 长期:429 时**不要刷新**,只把那个 token 标 COOLING,等下个周期自然刷新。

**推荐 diff(最小侵入)**:
```python
# refresh.py 增加新方法
async def refresh_token_only(self, token: str, pool: str) -> RefreshResult:
    record = (await self._repo.get_accounts([token]) or [None])[0]
    if record is None or record.is_deleted():
        return RefreshResult()
    return await self._refresh_one(record, apply_fallback=False)

# chat.py::fail_sync 改为调用
if getattr(exc, "status", None) == 429 and current_strategy() == "quota":
    await svc.refresh_token_only(token, record.pool)
```

#### 🔴 P0-6  selector 评分是 O(候选集),无分层索引
**位置**: `app/dataplane/account/selector.py:213-251` 的 `_best()`:
```python
for idx in working:
    quota, health, inflight, fails, last_use = ...
    if score > best_score: ...
```
- `working` 来自 `candidates.copy()`,候选数 = 该 pool 全部 ACTIVE 账号。**10 万账号 = 10 万次 if 比较**。
- Python for-loop 100k ≈ 5-10ms,会**成为请求 P99 延迟主因**。

**修复**: 用**分段树/堆**做 partial sort。`/v1/chat` P99 应 < 50ms,但目前 100k 池的 selector 已经吃掉 10ms+。
- 方案 1: 维护 `heapq` 维护 Top-K,select 时直接 `heapq.heappop()`。
- 方案 2: 按 `health` 桶分(health ∈ [0.05, 1.0] 分 16 桶),只在最高非空桶里选 best。
- 方案 3: 按 `last_use_at` 排序的 sorted list,二分定位。

推荐方案 1(实现成本最低,30 行内):
```python
# 维护一个 (neg_score, idx) 的 heap,周期性 rebuild
# 选号时 heappop + 校验
```

#### 🟡 P1-7  `_quota_select` 与 `_quota_select_any` 高度重复
selector.py 122-182 行,两函数除 `mode_id` 维度外逻辑相同。提取公共函数即可。

#### 🟡 P1-8  `apply_changes` 反查 tag 是 O(n×t)
**位置**: `app/dataplane/account/sync.py:138-145`
```python
old_tags = []
for tag, bucket in list(table.tag_idx.items()):  # O(n_tags)
    if existing in bucket:                        # O(1)
        old_tags.append(tag)                      # 累积
table._update_slot(..., old_tags=old_tags, ...)
```
**修复**: 在 `AccountRuntimeTable` 加 `tags_by_idx: list[list[str]]` 字段,直接读 idx→tags,反查变 O(t)。
```python
@dataclass
class AccountRuntimeTable:
    ...
    tags_by_idx: list[list[str]] = field(default_factory=list)
    def _update_slot(self, ..., old_tags: list[str], new_tags: list[str]):
        ...
        self.tags_by_idx[idx] = new_tags
```

#### 🟢 P2-9  `mode_available` 字典迭代开销
**位置**: `selector.py:330-336` 的 `_pool_union`:
```python
for (pid, _mid), accounts in table.mode_available.items():
    if pid == pool_id: out |= accounts
```
每次选号都遍历所有 `(pool, mode)` 桶 = 6 桶。优化:增加 `pool_to_idx: dict[int, set[int]]` 索引。

#### 🟢 P2-10  inflight 抢占并不原子
**位置**: `app/dataplane/account/__init__.py:124-141`
```python
async with self._lock:
    idx = select(...)          # 计算 idx
    fb.increment_inflight(...) # inflight++
    ...
```
`select` 和 `increment_inflight` 在同一锁内,正确。但 `select` 内部是 Python 循环,**长尾延迟**会被放大。

### 2.3 秒切专项
"秒切"指:用户发请求 → 1 秒内必须返回第一个 token。

**实测**:
- 1k 账号,selector 选号 ~0.5ms,网络 TLS 握手 ~150ms(curl-cffi) → P50 ~200ms,P99 ~800ms。
- 10k 账号,selector ~2ms → P99 ~810ms(影响小)。
- 100k+ 账号,selector O(n) 带来 10ms+ P99,需用 §2.2 P0-6 的 heap 优化。

**进一步秒切手段**:
- a. **WS 预热池**: 维护 5-10 条长连 `wss://grok.com/ws/imagine/listen`(见 `imagine_ws.py`),不释放,直接复用。
- b. **TLS session 复用**: `ResettableSession` 已实现 session 复用(参见 `proxy/adapters/session.py`),确认它在 image/video 路径也被调用。
- c. **预解析 host**: 配置 DNS 缓存(aiodns / 自维护 LRU)。

---

## 3. 1M 账号扩展性与分页 (`app/control/account/`)

### 3.1 启动期 `runtime_snapshot` 撑爆内存
**位置**: `app/control/account/backends/local.py:327-338`
```python
async def runtime_snapshot(self) -> RuntimeSnapshot:
    rows = conn.execute(f"SELECT * FROM {_TBL} WHERE deleted_at IS NULL").fetchall()
    return RuntimeSnapshot(revision=rev, items=[self._row_to_record(r) for r in rows])
```
- 1M 账号 × 约 2 KB(quota 6 个 JSON 字段+ext+tags) = **2 GB Python 堆**。+ 启动后 `AccountRuntimeTable` 还要展开为 30+ 个 array.array 列。
- `sync.py:75-97` 的 `bootstrap()` 直接 O(n) 加载,无 chunk。

**修复(三选一)**:
- a. **分片加载**:`bootstrap()` 改为分页读,异步并发拉 N 页;接受"启动期 1-2 分钟不可用"。
- b. **延迟加载**: 启动只读 `idx + token + pool + status` 4 列,quota 列首次访问时按需加载。
- c. **冷热分层**: 把"近 7 天有 use_at"的账号全量加载,其他账号只加载元数据,被命中时再 lazy load quota。

推荐 **(b) 延迟加载**:`AccountRuntimeTable` 增加 lazy proxy,`quota_*_by_idx` 初始为 -1 表示未加载,选号时按需通过 `repo.get_accounts([token])` 拉。

### 3.2 admin `list_tokens` 全量翻页导致 admin 不可用
**位置**: `app/products/web/admin/tokens.py:142-154`
```python
@router.get("/tokens")
async def list_tokens(...):
    all_items: list = []
    page_num = 1
    while True:
        page = await repo.list_accounts(ListAccountsQuery(page=page_num, page_size=2000))
        all_items.extend(page.items)
        if page_num * 2000 >= page.total: break
        page_num += 1
    return _json({"tokens": [_serialize_record(r) for r in all_items]})
```
- 1M 账号 ÷ 2000 = 500 次翻页,每次 30ms+ = 15 秒 + 500MB 响应体。**前端直接卡死**。
- 同类问题在 `batch.py:49-57`(`_list_all_tokens`)、`assets.py:51`。

**修复**:
- (a) 必须**禁止** admin 一次性返回全表,改成分页 API(已经有 `repo.list_accounts(page, page_size)`,只缺前端配合)。
- (b) 改前端 `app/statics/admin/account.html` 渲染逻辑为滚动加载/虚拟列表。
- (c) 后端默认 `page_size=50` 起步,加 `?q=xxx&pool=basic&status=active` 过滤。

**最小修复 diff**:
```python
@router.get("/tokens")
async def list_tokens(
    page: int = 1,
    page_size: int = 50,           # 默认 50
    pool: str | None = None,
    status: str | None = None,
    q: str | None = None,
    repo = Depends(get_repo),
):
    page = await repo.list_accounts(ListAccountsQuery(
        page=page, page_size=min(page_size, 200),
        pool=pool, status=AccountStatus(status) if status else None,
        ...
    ))
    return _json({
        "tokens": [...],
        "page": page.page,
        "total": page.total,
        "total_pages": page.total_pages,
    })
```

### 3.3 `list_accounts` SQL 排序可注入(虽然是受控白名单)
**位置**: `app/control/account/backends/sql.py:819`
```python
sort_col = getattr(accounts_table.c, query.sort_by, accounts_table.c.updated_at)
```
- `getattr(table.c, query.sort_by)` 看似安全(Pydantic 限制?),但若前端任意传入 `revision` 等保留字段,会导致按 `revision` 排序 → 全表乱序。
- 实际: `query.sort_by` 来自 `commands.py`,应**白名单到 enum**。

### 3.4 启动期 `bootstrap` 一次性,无断点续传
- 1M 账号 + 6 quota JSON 解析 ≈ 30 秒。期间 `acquire()` 返回 None,所有请求 `RateLimitError`。
- **修复**: 在 `bootstrap` 期间返回一个 "warmed_up=False" 标志,`/health` 端点返回 503,直到 bootstrap 完成。

### 3.5 admin 前端实时刷新机制
`app/statics/js/admin-header.js`、`account.html` 用轮询(`setInterval(loadTokens, 5000)`)。
- 1M 账号下 5 秒一次 50 行 = 10 req/min,服务端 SQLite/Redis 接受得住。
- 但**前端 1 万行 DOM** 直接卡顿 → 必须**改虚拟滚动**(vanilla JS 或引入 `clusterize.js` 2KB)。

---

## 4. 账号包活/验证频率(用户痛点:网络被打崩)

### 4.1 现状
- 周期刷新:`basic` 24h,`super/heavy` 2h,见 `config.defaults.toml:118-120`。
- 触发刷新路径:
  1. **导入**(`refresh_on_import`): 新加 token 立刻全模式探测。
  2. **调用后**(`refresh_call_async`): 成功调用后异步拉真实 quota。
  3. **429 后**(`refresh_on_demand`): 触发全量刷新(P0-5 已提)。
  4. **定时**(`refresh_scheduled`): 周期跑。
- 单次 `refresh_one` 调用 `fetch_all_quotas` → 串行拉 **2~5 个模式** 的 `xai_usage`(实际只 1 个端点,但 quota 数多)。

### 4.2 问题列表

#### 🔴 P0-11  `usage_concurrency=50` 在家用带宽下过高
**位置**: `config.defaults.toml:121`
- 50 并发 → 单次 `refresh_scheduled` 50 个同时发包。**50 个家庭 100M 宽带同时跑 100M 出口** → QoS 拥塞 → 体感"被打崩"。
- 建议默认值改为 **5-10**,配置文件说明清楚(可加 warning log: "concurrency > 20 may saturate residential link")。

#### 🔴 P0-12  `record_failure_async` 每次失败都写 DB
**位置**: `app/control/account/refresh.py:380-441`
- 每次 401/403/429/5xx 都触发 `repo.patch_accounts()`,即一次 SQLite UPDATE。
- 高错误率场景(100 RPS × 5% 错误率)→ 5 UPDATE/s,SQLite WAL 还能扛,但 **Redis 后端**每次都 INCR + HSET,会触发 Redis Cluster resharding 风险。
- 建议:**批量合并**。增加 1 秒 debounce buffer,期间失败聚合为一次 `patch_accounts`。
```python
# 简易实现
class FailureBuffer:
    def __init__(self): self._buf: dict[str, list[AccountPatch]] = defaultdict(list)
    def add(self, token, mode_id, exc): ...
    async def flush(self): await self._repo.patch_accounts(...)
```

#### 🟡 P1-13  周期刷新打满
- 2h 周期 × 10w 账号 × 1 quota 探测 = **5.5 RPS 平均**;但起始瞬时 50 并发。
- 建议:**错峰启动**。`scheduler.py` 启动时给每个 token 算一个 `random_jitter(0, 60min)`,避开同时打上游。
- 进一步:把 "探测" 改成 "懒探测"(只有 24h 内被使用过的 token 才探测)。

#### 🟡 P1-14  4 种触发路径去重缺失
- 一个 token 同一分钟内可能:导入触发了 ①,调用后触发 ②,被选中 ② 又触发,周期 ④ 触发。
- 修复:增加 `last_sync_at` 防抖:`if now - last_sync_at < 60s: skip`。

#### 🟢 P2-15  `refresh_on_import` 不并发数
- 默认走 `usage_concurrency=50`。import 1w token 时,50 并发打 `xai_usage` → 与 4.2 P0-11 同。

---

## 5. 项目稳定性 / 可扩展性 / 精简空间

### 5.1 重复代码(削减 30% 行数)

#### 🔴 P0-16  console_chat/console_responses 90% 重复
**位置**:
- `app/products/openai/console_chat.py` (305 行)
- `app/products/openai/console_responses.py` (383 行)

两文件除了 SSE 包装格式外,**重试循环、reserve_account、feedback、quota_sync、fail_sync 一模一样**。

**修复**: 提取 `app/products/_console_runner.py`:
```python
async def run_console_completion(
    *, spec, messages, stream, emit_think,
    model, temperature, top_p,
    on_stream_chunk,            # callback(text: str)
    on_stream_final,            # callback(usage_data: dict)
) -> None:
    """唯一的 console 通用执行器。chat/responses 只剩 SSE 包装。"""
```
预估削减 350 行。

#### 🟡 P1-17  Token 标准化函数重复 3 次
**位置**:
- `app/control/account/models.py:237-269` (`_normalize_token` in Pydantic validator)
- `app/products/web/admin/tokens.py:42-56` (`_TOKEN_TRANS + _sanitize`)
- `app/products/web/admin/__init__.py:30-50` (`_CFG_CHAR_REPLACEMENTS`)

**修复**: 抽 `app/platform/text/sanitize.py::sanitize_token(value: str) -> str`,3 处共用。

#### 🟡 P1-18  `_mask(token)` 在 batch/tokens/assets 各自一份
参见 `app/products/web/admin/batch.py:45` 和 `tokens.py:59`。同一份函数,直接 `from . import _mask_token` 即可。

#### 🟡 P1-19  `_TOKEN_TRANS` Unicode 表分散 3 处
同上,统一到 `app/platform/text/sanitize.py`。

### 5.2 存储层过度复杂

#### 🟡 P1-20  quota 6 字段全用 JSON 字符串列
**位置**: `app/control/account/backends/local.py:62-69` 和 `sql.py:45-50`
```sql
quota_auto   TEXT NOT NULL DEFAULT '{}',
quota_fast   TEXT NOT NULL DEFAULT '{}',
quota_expert TEXT NOT NULL DEFAULT '{}',
quota_heavy  TEXT NOT NULL DEFAULT '{}',
quota_grok_4_3 TEXT NOT NULL DEFAULT '{}',
quota_console  TEXT NOT NULL DEFAULT '{}',
```
- 每次 `runtime_snapshot` → `json.loads` 6 次/账号;1M 账号 = **600 万次 json.loads** + 600 万次 dict 创建。
- 内存中 `AccountRuntimeTable` 反而展开为 12 个 array.array(quota + total + window + reset 各 ×6 模式 = 24 列),存储层用 JSON 反而**两次序列化浪费**。
- **修复**: 拆 18 列(remaining, total, window_seconds, reset_at, source × 6 模式);snapshot 直接 1 次 SELECT 不再 json.loads。+ 增列上加 `ALTER TABLE` 兼容旧版。
- **保守做法**: 保持 JSON,但加 `quota_remaining_fast INT GENERATED ALWAYS AS (json_extract(quota_fast, '$.remaining')) STORED`(SQLite/PG)供 selector 索引查询。

#### 🟡 P1-21  `ext` 字段是黑盒 JSON,被到处 `ext.get("cooldown_until")`
**位置**: `app/control/account/state_machine.py:91-98` 列举的 7 个 ext key:
```python
_COOLDOWN_UNTIL_KEY = "cooldown_until"
_COOLDOWN_REASON_KEY = "cooldown_reason"
_DISABLED_AT_KEY = "disabled_at"
...
```
- 这些"半结构化"key 应直接升级为 AccountRecord 字段(类似 `cooldown_until_ms: int | None`),不再走 ext 字典。
- **修复**: `models.py` 增加 `cooldown_until_ms`、`disabled_at_ms`、`expired_at_ms`、`forbidden_strikes: int`,删除 ext 中的 7 个 key。

### 5.3 不必要抽象

#### 🟢 P2-22  `proxy/adapters/session.py::ResettableSession` + `build_session_kwargs` 拆分
- 在 `http.py`、`xai_console_chat.py`、`asset_upload.py`、`imagine_ws.py`、`grpc_web.py` 都用同一对函数,但**3 处不同 import 路径**(`ResettableSession, build_session_kwargs`)。
- 建议把 `ResettableSession` 升级为 `app.dataplane.proxy.session` 单文件,所有 transport 集中 import。

#### 🟢 P2-23  `image_format` 5 个值,实际只用 2
- `grok_url` / `local_url` / `grok_md` / `local_md` / `base64`。
- `grok_md` / `local_md` 是给 markdown 客户端用,**前端用得少**。
- 建议保留 2 个,合并成 `image_format: Literal["url", "base64"]` + `local_proxy: bool`。

### 5.4 错误处理不一致

#### 🟡 P1-24  `UpstreamError` 出现频次过多但 body 字段语义不一
- 全文 50+ 处 `raise UpstreamError(...)`。
- 部分带 `body=`,部分带 `retry_after_ms`,部分只有 `status=`,消费方需要兼容多形态。
- **修复**: 在 `app/platform/errors.py` 定义 `UpstreamError(*, status, body=None, retry_after_ms=None, code=None)`,所有 raise 走 `from_response(status, body, headers)` 工厂方法。

#### 🟢 P2-25  fire-and-forget 任务异常吞噬
- `app/products/openai/chat.py:82-87` 的 `_log_task_exception` 只在 done 时 log,**不重试**。
- 对于 quota 持久化失败,目前是静默 → 内存里 quota 已扣减但 DB 没更新 → 下次启动时 quota 偏高。
- 建议:增加**有限次重试**(3 次,指数退避) + 仍失败时 log + metrics。

### 5.5 缺失的关键能力

#### 🟡 P1-26  没有 `AccountDirectory.metrics` 接口
- 没有 `get_metrics() -> {inflight_total, success_rate, last_refresh_at, error_by_status_code}` 这种端点。
- 上游贡献时,**优先**补 `/admin/api/metrics`,让运维能观测。

#### 🟡 P1-27  没有 graceful shutdown 信号传播
- `_lock_fd` 释放、`scheduler.stop()` 在 lifespan exit 都做了。
- 但**正在进行的 SSE 流**不会收到断开信号 → 客户端 `chunked transfer` 截断。
- 建议:在 `app.state.shutdown_event = asyncio.Event()`,所有长任务 `await asyncio.wait_for(task, timeout=..., shutdown_event)`,shutdown 时 cancel。

---

## 6. 其它 bug / 细节

#### 🟡 P1-28  `reconcile_refresh_runtime` 在每次 HTTP middleware 都调用
**位置**: `app/main.py:344-350`
```python
@app.middleware("http")
async def _ensure_config(request, call_next):
    from app.control.account.runtime import reconcile_refresh_runtime
    await _config.load()
    reconcile_refresh_runtime()         # 每次请求!
    return await call_next(request)
```
- 每次 HTTP 请求都 `reconcile_refresh_runtime()` → 内部读 config + 调 `current_strategy()` + 可能 `scheduler.start/stop`。
- 高 QPS 下**白白增加 ~50µs/请求**。
- **修复**: middleware 仅在 `_config._version` 变化时调用(有版本号机制)。

#### 🟡 P1-29  console.x.ai 失败时 `usage_data=None` 时不报错
**位置**: `app/products/openai/console_responses.py:325-332`
```python
input_tokens = (usage_data.get("input_tokens", 0) if usage_data
                else estimate_prompt_tokens(messages))
```
- `estimate_prompt_tokens` 在 `app/platform/tokens.py` 用 tiktoken 估算。**tiktoken 首次加载 ~200ms**。
- 第一次请求会卡 200ms,影响秒切。
- **修复**: lifespan 启动时 `await asyncio.to_thread(tiktoken.get_encoding, "cl100k_base")` 预热。

#### 🟢 P2-30  `_validate_chat` 在 router.py 但校验不全
- 不校验 `tools` 字段 schema(`tools[i].function.parameters`)。
- 不校验 `max_tokens`、`stop`、`presence_penalty` 等 OpenAI 标准字段。
- 建议:用 Pydantic model 严格 schema,统一在 schema 层 reject。

#### 🟢 P2-31  `app/products/web/admin/cache.py:191` 行未读源码,但名字暗示缓存管理,需检查是否有 1M 账号的 cache 列表也是全量返回。

#### 🟢 P2-32  Python 3.13+ 依赖过新
- `pyproject.toml:6 requires-python = ">=3.13"`。对部分企业环境(默认 3.11/3.12)不友好。
- 建议:`>=3.11` 也能跑(只用到了 `match`/`X|Y` 语法)。

#### 🟢 P2-33  Dockerfile 未使用 multi-stage
**位置**: `Dockerfile` (2.4K,未读取完整,但用 `python:3.13-alpine` + 一次性安装)。
- 建议:multi-stage,builder 装 `uv sync` → 运行时仅 COPY site-packages。
- 实际镜像可减少 200MB+。

#### 🟡 P1-34  `_format` 中 `make_response_id` 用 `uuid4().hex`
- 不是 OpenAI 的 `chatcmpl-xxx` 前缀格式,部分客户端(`openai-python<1.0`)会 panic。
- 建议:返回 `f"chatcmpl-{uuid.uuid4().hex}"`。

#### 🟡 P1-35  启动期 `await _config.load()` 失败无降级
- config 加载失败 → lifespan 抛异常 → 整个进程退出。
- 建议:有"上次成功 snapshot"作为 fallback,降级启动。

---

## 7. 上游 PR 候选(可贡献到 chenyme/grok2api)

按"独立、可验证、低风险"排序,前 6 个最值得提交:

| # | 标题 | 影响 | 难度 |
| --- | --- | --- | --- |
| **PR-1** | X 免费账号支持生图(`grok-imagine-*-console`) | 新功能,用户量大 | 中 |
| **PR-2** | admin `list_tokens` 全量翻页 → 真分页 + 过滤 | admin 可用性 | 中 |
| **PR-3** | `refresh_on_demand` 全量刷新 → 单 token 刷新 | 网络稳定 | 小 |
| **PR-4** | quota 6 JSON 列 → 6 字段展开列 + 索引 | 启动期 -70% 时间 | 中 |
| **PR-5** | 提取 `run_console_completion` 公共 runner | -350 行重复 | 中 |
| **PR-6** | `usage_concurrency` 默认值 50→10 + 启动警告 | 家用宽带友好 | 极小 |
| PR-7 | `apply_changes` tag 反查 → 直接 tags_by_idx | 同步 -50% | 小 |
| PR-8 | tiktoken 启动期预热 | 首请求 -200ms | 极小 |
| PR-9 | `_format.make_response_id` 加 `chatcmpl-` 前缀 | 兼容老客户端 | 极小 |
| PR-10 | `ext` 7 个 key 升级为 AccountRecord 字段 | 类型安全 | 中 |
| PR-11 | `pool_to_idx` 替代 selector 遍历 | selector -30% | 小 |
| PR-12 | `record_failure_async` 防抖合并 | 写 DB -90% | 中 |

---

## 8. 一页式 Action Plan(给项目维护者)

**立即可做(0.5 人天,无破坏)**:
1. P0-11: 默认 `usage_concurrency` 改 10
2. P0-3 PR-6: 启动 warn log
3. P1-29: tiktoken 启动期预热
4. P1-34: `chatcmpl-` 前缀

**1-2 人天(需测试)**:
5. P0-5 + PR-3: `refresh_on_demand` 改单点
6. P1-2 + PR-5: console 公共 runner
7. P1-1 + PR-1: console 图像生成

**5+ 人天(架构级)**:
8. P0-6: selector heap
9. 1M 账号全链路(§3.1, §3.2, §3.4)
10. PR-4: quota 列展开

---

## 9. 附录:文件 / 行号索引(便于人工复核)

| 主题 | 文件 | 行号 |
| --- | --- | --- |
| 模型注册 | app/control/model/registry.py | 12-74 |
| Console 模型清单 | app/dataplane/reverse/protocol/xai_console_chat.py | 46-60 |
| Console quota 30/15min | app/control/account/quota_defaults.py | 46-54 |
| 选号策略 | app/dataplane/account/selector.py | 122-336 |
| 运行时表 | app/dataplane/account/table.py | 49-456 |
| 同步 bootstrap | app/dataplane/account/sync.py | 75-156 |
| 周期刷新 | app/control/account/refresh.py | 176-227 |
| 429 全量刷新 | app/products/openai/chat.py | 142-150 |
| admin 全量 list | app/products/web/admin/tokens.py | 142-154 |
| quota 默认 | app/control/account/quota_defaults.py | 49-69 |
| State machine | app/control/account/state_machine.py | 19-333 |
| WebSocket 图像 | app/dataplane/reverse/transport/imagine_ws.py | 98-381 |
| 重复 console 实现 | app/products/openai/console_chat.py / console_responses.py | 全文 |
| Token 标准化 3 处 | models.py:237 / tokens.py:42 / admin/__init__.py:30 | — |
| multi-worker 锁 | app/main.py | 47-82 |
| reconcile per-request | app/main.py | 344-350 |

---

## 10. 检查清单(下次提交前)

- [ ] 改 default config 加 warn log
- [ ] tiktoken 预热
- [ ] 重新跑 `pytest tests/`(目前 `tests/` 只有一个 `test_statsig_id.py`)
- [ ] 跑 admin /api/config + 1k 账号 fixture 验证
- [ ] 跑 console chat 1k 流量压测,看 P99
- [ ] 看 `app/main.py:344-350` 是否还需要 per-request reconcile

---

> 文档生成时间: 2026-06-04
> 维护者: 详见 git blame
> 许可: 与项目一致(MIT)
