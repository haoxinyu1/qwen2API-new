# qwen2API 优化实施文档

## 文档目的

本文档用于指导后续开发者或 AI 模型对 qwen2API 进行结构化优化。文档基于对 [ds2api](https://github.com/CJackHwang/ds2api) 项目源码的深度分析，提取出其中适用于 qwen2API 的架构模式和工程实践，结合 qwen2API 自身特点（浏览器引擎、千问上游），给出具体的实施方案。

本文档的目标不是重写项目，而是在保留当前可用能力的前提下，逐步收敛重复逻辑、统一内部语义、增强诊断能力。

---

## 一、当前项目结构

```text
qwen2API/
├── backend/
│   ├── api/                    # 协议适配层（路由入口）
│   │   ├── v1_chat.py          # OpenAI /v1/chat/completions
│   │   ├── anthropic.py        # Anthropic /anthropic/v1/messages
│   │   ├── gemini.py           # Gemini /v1beta/models/*
│   │   ├── images.py           # 图片生成 /v1/images/generations
│   │   ├── admin.py            # 管理 API
│   │   └── probes.py           # 健康检查
│   ├── core/                   # 运行时核心
│   │   ├── config.py           # 配置与模型映射
│   │   ├── account_pool.py     # 账号池
│   │   ├── hybrid_engine.py    # 混合引擎（浏览器 + httpx）
│   │   ├── browser_engine.py   # Camoufox 浏览器引擎
│   │   └── httpx_engine.py     # httpx / curl_cffi 引擎
│   └── services/               # 业务服务
│       ├── qwen_client.py      # 千问上游客户端
│       ├── tool_parser.py      # 工具调用解析
│       ├── prompt_builder.py   # Prompt 组装
│       └── auth_resolver.py    # 凭证自愈
├── frontend/                   # React + Vite 管理台
├── docs/                       # 文档
├── data/                       # 运行时数据（账号、Key、配置）
├── Dockerfile
├── docker-compose.yml
└── start.py
```

### 当前存在的主要问题

1. `v1_chat.py`、`anthropic.py`、`gemini.py` 各自独立实现了 stream 组装、tool call 处理、重试、finish_reason 判定等业务逻辑，存在大量重复。
2. Tool Calling 的解析、防循环、回注、流式事件拼装分散在多个文件中，不同协议的行为可能不一致。
3. 缺少统一的内部请求/响应/事件结构，协议层和业务层耦合。
4. 运行时诊断信息不足，无法快速判断引擎状态、fallback 情况、账号池健康度。
5. 配置的静态/动态边界不清晰。
6. 缺少自动化测试和面向开发者的架构文档。

---

## 二、参考 ds2api 的关键架构模式

以下是 ds2api 中值得借鉴的具体实现模式。

### 2.1 统一执行核心模式

ds2api 的做法：

- Claude adapter（`internal/adapter/claude/handler_messages.go`）不直接调用上游，而是通过 `translatorcliproxy.ToOpenAI()` 把 Claude 请求转换成 OpenAI 格式，然后**直接调用 OpenAI adapter 的 `ChatCompletions` 方法**。
- Gemini adapter 也是同样的模式。
- 只有 OpenAI adapter（`internal/adapter/openai/handler_chat.go`）真正执行上游调用。

结果：所有协议共享同一条执行路径，tool calling、streaming、retry、finish_reason 只实现一次。

### 2.2 StandardRequest 统一请求结构

ds2api 定义了 `util.StandardRequest`，所有协议适配层都把原始请求转成这个统一结构：

```go
type StandardRequest struct {
    Surface        string        // "openai_chat" / "openai_responses" / "claude" / "gemini"
    RequestedModel string        // 客户端传入的模型名
    ResolvedModel  string        // 实际使用的上游模型
    ResponseModel  string        // 返回给客户端的模型名
    Messages       []any
    FinalPrompt    string        // 组装后的最终 prompt
    ToolNames      []string      // 可用工具名列表
    ToolChoice     ToolPolicy
    Stream         bool
    Thinking       bool
    Search         bool
    PassThrough    map[string]any
}
```

### 2.3 Tool Calling 统一解析层

ds2api 的 `internal/toolcall/` 目录是独立的工具调用解析模块：

- `toolcalls_parse.go`：统一入口 `ParseToolCallsDetailed()`
- 支持 JSON、XML、Markup、TextKV 四种格式
- 输出统一结构 `ParsedToolCall{Name, Input}`
- `tool_sieve_core.go`：流式内容中的工具调用检测与切分（增量消费）
- `tool_sieve_state.go`：流式工具检测的状态机

### 2.4 配置静态/动态分离

ds2api 的 `internal/config/config.go`：

- `Config`：静态配置（keys、accounts、proxies、model_aliases、admin 等）
- `RuntimeConfig`：运行时可热更新（account_max_inflight、account_max_queue、global_max_inflight、token_refresh_interval_hours）
- `applyRuntimeSettings()`：admin 修改后立即生效

### 2.5 版本检查接口

ds2api 的 `internal/admin/handler_version.go`：

- 返回当前版本、来源、构建时间
- 自动查询 GitHub Releases API，比较是否有新版本
- 返回 `has_update: true/false`

### 2.6 开发抓包

ds2api 的 `internal/devcapture/`：

- 运行时可开启/关闭请求录制
- 录制请求/响应原始内容
- 通过 admin API 查看和清除
- 用于调试上游 SSE 行为

### 2.7 测试体系

ds2api 的测试分为：

- 单元测试：Go `_test.go` 文件，覆盖 tool parsing、stream 行为、adapter 逻辑
- 测试夹具：`tests/compat/fixtures/` 下有 SSE chunk 和 toolcall 的输入/预期输出
- Raw SSE 样本：`tests/raw_stream_samples/` 录制真实上游 SSE 响应用于回放测试
- E2E 测试：独立 CLI 工具 `cmd/ds2api-tests/`，启动隔离服务后执行全链路测试

### 2.8 文档体系

ds2api 维护以下独立文档：

- `docs/ARCHITECTURE.md`：完整目录结构 + 模块职责 + 请求主链路 mermaid 图
- `docs/DEPLOY.md`：部署指南
- `docs/TESTING.md`：测试指南（含 CLI 参数、产物结构、自动清理）
- `docs/toolcall-semantics.md`：工具调用解析语义的完整说明
- `docs/DeepSeekSSE行为结构说明-2026-04-05.md`：上游 SSE 行为记录
- `API.md`：接口文档

---

## 三、优化项 1：统一协议执行核心

### 目标

让 `v1_chat.py`、`anthropic.py`、`gemini.py` 只负责协议格式转换，所有业务逻辑集中到一个执行核心。

### 新增文件

#### `backend/schemas/chat_types.py`

定义以下数据类（使用 dataclass 或 Pydantic）：

```python
@dataclass
class StandardRequest:
    surface: str              # "openai" | "anthropic" | "gemini"
    requested_model: str      # 客户端传入的模型名
    resolved_model: str       # 实际使用的千问模型
    response_model: str       # 返回给客户端的模型名
    messages: list[dict]      # 统一格式的消息列表
    system_prompt: str        # 系统 prompt
    user_content: str         # 最终发送给千问的用户内容
    stream: bool
    tools: list[dict] | None  # 工具定义列表
    tool_names: list[str]     # 工具名列表
    has_custom_tools: bool
    temperature: float | None
    max_tokens: int | None
    metadata: dict            # 协议特有的透传字段

@dataclass
class StandardStreamEvent:
    type: str                 # "text_delta" | "tool_call_start" | "tool_call_delta" | "tool_call_end" | "message_end" | "error"
    content: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    tool_arguments_delta: str = ""
    finish_reason: str = ""
    usage: dict | None = None

@dataclass
class StandardResponse:
    text: str
    tool_calls: list[dict]
    finish_reason: str
    usage: dict
    images: list[str] | None = None
```

#### `backend/services/chat_gateway.py`

提供两个核心方法：

```python
class ChatGateway:
    def __init__(self, qwen_client: QwenClient):
        self.client = qwen_client

    async def execute(self, req: StandardRequest) -> StandardResponse:
        """非流式执行"""
        # 1. 判断是否为图片意图
        # 2. 构造 payload
        # 3. 调用 qwen_client
        # 4. 解析响应
        # 5. 返回 StandardResponse

    async def execute_stream(self, req: StandardRequest) -> AsyncGenerator[StandardStreamEvent, None]:
        """流式执行"""
        # 1. 判断是否为图片意图
        # 2. 构造 payload
        # 3. 调用 qwen_client.chat_stream_events_with_retry
        # 4. 把 qwen SSE 事件转成 StandardStreamEvent
        # 5. yield 每个事件
```

### 修改文件

#### `backend/api/v1_chat.py`

改为：

1. 解析 OpenAI 格式请求
2. 转成 `StandardRequest`
3. 调用 `chat_gateway.execute()` 或 `chat_gateway.execute_stream()`
4. 把 `StandardStreamEvent` 转成 OpenAI SSE 格式输出
5. 把 `StandardResponse` 转成 OpenAI JSON 格式输出

#### `backend/api/anthropic.py`

改为：

1. 解析 Anthropic 格式请求
2. 转成 `StandardRequest`
3. 调用 `chat_gateway`
4. 把 `StandardStreamEvent` 转成 Anthropic SSE block 格式输出
5. 把 `StandardResponse` 转成 Anthropic JSON 格式输出

#### Gemini 路由文件

同理。

### 需要迁移到 chat_gateway 的逻辑

以下逻辑当前分散在各协议文件中，需要统一迁移：

- 图片意图识别（关键词检测 → 路由到 T2I）
- 工具调用 prompt 注入（在 system prompt 中注入工具定义）
- 工具调用结果回传（把 tool_result 注回消息历史）
- 空响应重试
- finish_reason 判定（stop / tool_calls / length）
- 账号获取与释放

### 验收标准

- 三种协议的普通对话、工具调用、图片意图均正常工作
- `v1_chat.py` 体积减少 50% 以上
- `anthropic.py` 体积减少 50% 以上
- 新增一个 chat_gateway 调用场景时，不需要在三个协议文件中分别添加逻辑

---

## 四、优化项 2：统一 Tool Calling 中间层

### 目标

建立独立的工具调用模块，所有协议共享同一套解析、防循环、流式切分逻辑。

### 参考 ds2api 的实现

ds2api 的 `internal/toolcall/` 目录结构：

```text
toolcall/
├── toolcalls_parse.go          # 统一解析入口
├── toolcalls_parse_item.go     # 单个 tool call 解析
├── toolcalls_candidates.go     # 候选片段构建
├── toolcalls_format.go         # 格式检测
├── toolcalls_json_repair.go    # JSON 修复
├── toolcalls_markup.go         # Markup 格式解析
├── toolcalls_name_match.go     # 工具名匹配
├── toolcalls_textkv.go         # TextKV 格式解析
├── toolcalls_parse_markup.go   # XML/标签格式解析
├── toolcalls_input_parse.go    # 输入端工具定义解析
├── tool_prompt.go              # 工具 prompt 模板
└── *_test.go                   # 每个模块的测试
```

### 新增文件

#### `backend/services/tool_runtime.py`

```python
@dataclass
class ParsedToolCall:
    name: str
    arguments: dict
    arguments_raw: str        # 原始参数文本
    source: str               # "native" | "text_fallback" | "json_repair"

@dataclass
class ToolCallParseResult:
    calls: list[ParsedToolCall]
    saw_tool_syntax: bool     # 检测到工具调用语法
    rejected_by_policy: bool  # 被策略拒绝

class ToolRuntime:
    """统一工具调用运行时"""

    def parse_tool_calls(self, text: str, available_names: list[str]) -> ToolCallParseResult:
        """解析文本中的工具调用（支持 JSON / ##TOOL_CALL## / XML）"""

    def should_block(self, history: list, tool_name: str, arguments: dict) -> tuple[bool, str]:
        """防循环检测：相同工具+相同参数 ≥ 2 次则阻断"""

    def build_tool_prompt(self, tools: list[dict]) -> str:
        """为千问构建工具定义 prompt"""

    def inject_tool_result(self, messages: list, tool_call_id: str, name: str, content: str) -> list:
        """把工具结果注回消息历史"""
```

### 修改文件

#### `backend/services/tool_parser.py`

保留现有解析能力，但让返回值统一为 `ParsedToolCall` 和 `ToolCallParseResult`。

当前 `tool_parser.py` 中的以下函数需要统一：

- `parse_tool_calls()` → 返回 `ToolCallParseResult`
- `should_block_tool_call()` → 移到 `ToolRuntime.should_block()`
- `build_tool_blocks_from_native_chunks()` → 保留但返回统一结构
- `inject_format_reminder()` → 移到 `ToolRuntime.build_tool_prompt()`

#### `backend/services/prompt_builder.py`

工具 prompt 注入逻辑统一由 `ToolRuntime.build_tool_prompt()` 提供。

### 验收标准

- OpenAI 和 Anthropic 的工具调用行为完全一致
- 防循环逻辑在一个地方维护
- 所有工具调用解析结果都通过 `ParsedToolCall` 统一表示
- 新增工具调用解析格式时只需修改一个文件

---

## 五、优化项 3：增强诊断与运维能力

### 目标

参考 ds2api 的 version、dev capture、admin diagnostics，为 qwen2API 增加结构化诊断接口。

### 新增文件

#### `backend/core/metrics.py`

```python
class Metrics:
    """全局运行时指标收集器（内存，非持久化）"""

    def __init__(self):
        self.browser_requests = 0
        self.httpx_requests = 0
        self.browser_fallback_to_httpx = 0
        self.httpx_fallback_to_browser = 0
        self.waf_hits = 0
        self.t2i_success = 0
        self.t2i_failure = 0
        self.recent_errors: deque[dict] = deque(maxlen=50)
        self.create_chat_count = 0
        self.fetch_chat_count = 0
        self.delete_chat_count = 0
        self.start_time = time.time()

    def record_engine_request(self, engine: str): ...
    def record_fallback(self, from_engine: str, to_engine: str): ...
    def record_waf_hit(self): ...
    def record_t2i(self, success: bool): ...
    def record_error(self, source: str, message: str): ...
    def snapshot(self) -> dict: ...

# 全局单例
metrics = Metrics()
```

### 新增接口

#### `GET /version`

```python
@router.get("/version")
async def get_version():
    return {
        "version": VERSION,
        "python": sys.version,
        "engine_mode": settings.ENGINE_MODE,
        "uptime_seconds": int(time.time() - metrics.start_time),
    }
```

#### `GET /api/admin/diagnostics`

```python
@router.get("/api/admin/diagnostics")
async def get_diagnostics():
    return {
        "metrics": metrics.snapshot(),
        "engine": engine.status(),
        "account_pool": account_pool.status(),
    }
```

### 埋点位置

在以下位置调用 `metrics` 记录：

| 文件 | 埋点内容 |
|------|----------|
| `hybrid_engine.py` `api_call()` | browser/httpx 选择、fallback |
| `hybrid_engine.py` `fetch_chat()` | browser/httpx 选择、fallback |
| `browser_engine.py` | WAF 命中 |
| `qwen_client.py` `chat_stream_events_with_retry()` | 重试、账号切换、错误 |
| `qwen_client.py` `image_generate_with_retry()` | T2I 成功/失败 |
| `images.py` | 图片 URL 提取结果 |

### 验收标准

- 访问 `/version` 可看到版本和运行时间
- 访问 `/api/admin/diagnostics` 可看到 engine 统计、账号池状态、最近错误
- 能区分某次问题是 browser 还是 httpx 引起的

---

## 六、优化项 4：配置静态/动态分离

### 目标

参考 ds2api 的 `Config` + `RuntimeConfig` 分离模式。

### 实现方式

#### `backend/core/config.py`

在现有 `Settings` 类中增加注释分类：

```python
class Settings(BaseSettings):
    # ═══ 静态配置（需要重启生效）═══
    PORT: int = ...
    WORKERS: int = ...
    ENGINE_MODE: str = ...
    BROWSER_POOL_SIZE: int = ...
    ACCOUNTS_FILE: str = ...
    USERS_FILE: str = ...

    # ═══ 动态配置（可通过 admin API 热更新）═══
    MAX_INFLIGHT_PER_ACCOUNT: int = ...
    MAX_RETRIES: int = ...
    TOOL_MAX_RETRIES: int = ...
    EMPTY_RESPONSE_RETRIES: int = ...
    ACCOUNT_MIN_INTERVAL_MS: int = ...
    REQUEST_JITTER_MIN_MS: int = ...
    REQUEST_JITTER_MAX_MS: int = ...
    RATE_LIMIT_BASE_COOLDOWN: int = ...
    RATE_LIMIT_MAX_COOLDOWN: int = ...
```

#### `backend/api/admin.py`

admin settings 返回值中增加 `restart_required` 标记：

```python
{
    "settings": [
        {"key": "ENGINE_MODE", "value": "hybrid", "restart_required": true},
        {"key": "MAX_INFLIGHT", "value": 1, "restart_required": false},
        ...
    ]
}
```

#### `frontend/src/pages/SettingsPage.tsx`

对于 `restart_required: true` 的项，在 UI 中标注提示文字，例如"修改后需重启服务生效"。

### 验收标准

- 用户从 settings 页面能清楚知道哪些配置可以热更新
- 修改动态配置后立即生效，不需要重启
- 修改静态配置后页面提示需要重启

---

## 七、优化项 5：文档补全

### 目标

参考 ds2api 的文档体系，为 qwen2API 新增以下文档。

### `docs/architecture.md`

内容要求：

1. 完整目录结构（类似 ds2api 的目录树展开）
2. 请求主链路 mermaid 图
3. 各模块职责说明
4. HybridEngine 工作流程
5. Tool Calling 数据流
6. 图片生成数据流
7. 账号池状态机

### `docs/deployment.md`

内容要求：

1. Docker 拉取预构建镜像部署步骤
2. 本地源码运行步骤
3. `.env` 参数完整说明
4. `docker-compose.yml` 字段说明
5. 数据目录说明
6. 升级流程
7. 常见报错排查

### `docs/testing.md`

内容要求：

1. 编译检查命令
2. 单元测试执行方式
3. 手动接口测试方法（curl 示例）
4. 图片生成测试方法
5. 工具调用测试方法

### `docs/toolcall-semantics.md`

参考 ds2api 的同名文档，内容要求：

1. qwen2API 支持的工具调用格式（`##TOOL_CALL##`、native、JSON）
2. 解析管线说明
3. 防循环策略说明
4. 输出格式映射（内部结构 → OpenAI / Anthropic）
5. 已知边界与限制

---

## 八、优化项 6：测试补齐

### 目标

参考 ds2api 的测试夹具和回放机制，建立最低必要的自动化测试。

### 新增文件结构

```text
tests/
├── test_tool_parser.py         # 工具调用解析测试
├── test_images.py              # 图片 URL 提取测试
├── test_stream_events.py       # 流式事件映射测试
├── test_config.py              # 配置加载测试
├── fixtures/                   # 测试夹具
│   ├── sse_chunks/             # SSE 原始响应样本
│   ├── toolcalls/              # 工具调用输入/预期输出
│   └── image_responses/        # 图片生成 SSE 响应样本
└── conftest.py                 # pytest 公共配置
```

### 优先测试项

#### 1. Tool Parsing 测试

输入：各种格式的工具调用文本
预期：解析出正确的 `ParsedToolCall`

覆盖场景：

- `##TOOL_CALL##` 格式
- native tool_calls JSON 格式
- 非法 JSON（缺少引号、未转义换行）
- 相同工具重复调用阻断
- 代码块示例保护（不应解析为工具调用）

#### 2. Images URL 提取测试

输入：各种格式的图片响应文本
预期：正确提取出图片 URL

覆盖场景：

- Markdown 图片语法 `![alt](url)`
- JSON 字段 `"url": "https://..."`
- `extra.tool_result[].image` 格式
- `cdn.qwenlm.ai` 裸链接
- 无图片 URL 的纯文本响应

#### 3. Stream 事件映射测试

输入：qwen SSE 原始事件
预期：正确转成 `StandardStreamEvent`

覆盖场景：

- 普通文本 delta
- 工具调用增量
- finish 事件（stop / tool_calls）
- error 事件
- 图片生成 image_gen_tool 事件

### 验收标准

- `pytest tests/` 可以执行并全部通过
- 重构后可快速回归验证

---

## 九、建议实施顺序

### 第一阶段：低风险增强（不改现有逻辑，只新增）

| 序号 | 任务 | 涉及文件 |
|------|------|----------|
| 1 | 新增 `metrics.py` 全局指标收集器 | 新增 `backend/core/metrics.py` |
| 2 | 在 engine / client 中埋点 | `hybrid_engine.py`、`browser_engine.py`、`httpx_engine.py`、`qwen_client.py` |
| 3 | 新增 `/version` 和 `/api/admin/diagnostics` 接口 | `backend/api/probes.py`、`backend/api/admin.py` |
| 4 | 配置静态/动态分类注释 + admin 返回 `restart_required` | `backend/core/config.py`、`backend/api/admin.py` |
| 5 | 新增 `tests/` 目录和基础测试 | 新增 `tests/` |

### 第二阶段：结构统一（核心重构）

| 序号 | 任务 | 涉及文件 |
|------|------|----------|
| 6 | 新增 `chat_types.py` 统一数据结构 | 新增 `backend/schemas/chat_types.py` |
| 7 | 新增 `chat_gateway.py` 统一执行核心 | 新增 `backend/services/chat_gateway.py` |
| 8 | 新增 `tool_runtime.py` 统一工具层 | 新增 `backend/services/tool_runtime.py` |
| 9 | 重构 `tool_parser.py` 返回统一结构 | `backend/services/tool_parser.py` |

### 第三阶段：协议层瘦身

| 序号 | 任务 | 涉及文件 |
|------|------|----------|
| 10 | 重构 `v1_chat.py` 改为调用 gateway | `backend/api/v1_chat.py` |
| 11 | 重构 `anthropic.py` 改为调用 gateway | `backend/api/anthropic.py` |
| 12 | 重构 Gemini 路由改为调用 gateway | Gemini 路由文件 |

### 第四阶段：文档与回归

| 序号 | 任务 |
|------|------|
| 13 | 编写 `docs/architecture.md` |
| 14 | 编写 `docs/deployment.md` |
| 15 | 编写 `docs/testing.md` |
| 16 | 编写 `docs/toolcall-semantics.md` |
| 17 | 全链路回归测试 |

---

## 十、不应改动的部分

以下部分当前运行稳定，不建议在没有回归测试保障的情况下修改：

### 1. HybridEngine 路由策略

当前策略：

- `api_call()`：httpx 优先，browser fallback
- `fetch_chat()`：browser 优先，httpx fallback
- 出现 WAF / 401 / 403 / 429 时回退

这是 qwen2API 区别于 ds2api 的核心差异点。ds2api 面向 DeepSeek，不需要浏览器引擎；qwen2API 面向千问网页，浏览器引擎是反风控核心能力。

### 2. 图片生成链路

当前已能从 SSE 中 `extra.tool_result[].image` 提取真实 CDN 图片链接。这条链路不应重构，只允许增强。

### 3. 账号池状态机

当前支持 inflight、cooldown、invalid、pending_activation、banned 五种状态。可以增强（如增加统计），但不应重写核心逻辑。

### 4. Camoufox 浏览器初始化

`browser_engine.py` 中的 Camoufox 初始化、页面池管理、JS 执行方式已经过多轮调试验证，不应随意修改。

---

## 十一、给实现模型的执行要求

1. **按阶段实施**，禁止一次性修改所有协议文件。
2. 每完成一个阶段，必须验证以下场景：
   - OpenAI 流式对话
   - Anthropic 流式对话
   - 图片生成
   - Docker 启动
3. 不得删除 HybridEngine、图片生成链路、账号池现有逻辑。
4. 新增的统一层必须解决实际重复问题，不能为了抽象而抽象。
5. 每个新增文件必须附带至少一个基础测试。
6. 所有 Python 代码必须兼容 Python 3.12。
7. f-string 中不得包含反斜杠表达式（Python 3.11 及更早版本不支持）。
