# openai2claude — OpenAI ↔ Claude Adapter Proxy

> **本项目用于让 Claude Agent / Claude Code CLI 等只支持 Anthropic 协议的客户端，无缝调用任何 OpenAI 兼容 API 的模型（OpenAI / DeepSeek / GLM / Kimi / Qwen / 自部署 vllm 等）。**
>
> 你的客户端继续讲 Anthropic `/v1/messages`，本代理在本机做协议双向翻译后，向上游发 OpenAI `/v1/chat/completions`，并把流式响应实时翻译回 Anthropic SSE。Claude Code CLI 完全察觉不到对端是 OpenAI。
>
> 与 `../../claude_proxy/` 的目录结构和运维脚本完全对齐。

```
Claude Agent / Claude Code CLI
    │  Anthropic /v1/messages (SSE)
    ▼
openai2claude (本代理, 默认 :8080)
    │  OpenAI /v1/chat/completions (SSE)
    ▼
OpenAI / DeepSeek / GLM / Kimi / Qwen / vllm / ollama / ...
```

适用场景：
- 用 OpenAI / DeepSeek / GLM / Kimi 等模型驱动 Claude Code、Claude Agent SDK、Claude Desktop。
- reasoning 模型（gpt-5 / o-series / deepseek-v4-pro / kimi-k2-thinking / glm-4.6-thinking 等）的 `reasoning_content` 自动翻译成 Anthropic 原生 `thinking` block，避免下游报 `Error: No assistant messages found`。
- 想用 Claude Code 的工具（Read/Edit/Bash/...）+ 别家模型的能力时的标配适配层。

## 1. 它解决什么问题

`claude_proxy` 是一个 **Anthropic ↔ Anthropic** 的纯转发代理（多上游路由 + 热更新）。
本目录下的 `openai2claude` 在它之上做了一件本质上不同的事：**协议翻译**。
对外部署成 Anthropic 形态（Claude Code CLI 直接 `ANTHROPIC_BASE_URL` 指过来即可），
对上游则发 OpenAI Chat Completions 请求。

主要翻译能力：

| 维度                 | Anthropic（外部）                        | OpenAI（上游）                                      |
| -------------------- | ---------------------------------------- | --------------------------------------------------- |
| 路径                 | `POST /v1/messages`                      | `POST /v1/chat/completions`                         |
| 系统提示             | `system` 字段                            | `role: "system"` 消息                               |
| 工具                 | `tools[].input_schema`                   | `tools[].function.parameters`                       |
| 工具调用             | `content[].type=="tool_use"`             | `assistant.tool_calls[].function`                   |
| 工具结果             | `content[].type=="tool_result"`          | `role: "tool"` 消息                                 |
| 流式                 | `message_start` / `content_block_*` /<br>`message_delta` / `message_stop` | `chat.completion.chunk` deltas                       |
| 终止原因             | `stop_reason: end_turn / max_tokens / tool_use` | `finish_reason: stop / length / tool_calls`         |
| 用量                 | `usage.input_tokens / output_tokens`     | `usage.prompt_tokens / completion_tokens`（流式需 `stream_options.include_usage`） |

## 2. 目录结构

```
adapter/openai2claude/
├── proxy.py                  # 主程序（HTTP 服务 + 协议翻译）
├── config.json.template      # 配置模板（需复制为 config.json）
├── config.json               # 你的真实配置（gitignore）
├── README.md
├── PLAN.md
├── bin/
│   ├── _proc_lib.sh          # 共享进程识别 helper（被 start/stop/status 引用）
│   ├── start.sh              # 启动（PID 文件 + ps 兜底 + 端口三重预检）
│   ├── stop.sh               # 停止（pid 缺失/失效时自动按 cwd 兜底找回进程）
│   ├── restart.sh            # 重启（stop + start，参数转发）
│   ├── status.sh             # 状态 + HTTP 探活 + log 列表
│   └── reload_config.sh      # 调用 /admin/reload 热更新
└── log/                      # 运行时日志、PID、stdout/err
    ├── proxy.pid
    ├── proxy.out
    ├── proxy.err
    ├── proxy.log
    └── proxy.log.{1..6}      # 自动滚动
```

## 3. 配置

```bash
cp config.json.template config.json
$EDITOR config.json
```

每个 key 是 **「Claude CLI 里看到的 Claude 风格 model id」**，value 描述上游：

```json
{
    "claude-opus-4-x-via-openai": {
        "mapping_model": "opus",
        "OPENAI_API_KEY": "sk-...",
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "upstream_model": "gpt-5"
    },
    "claude-sonnet-4-x-via-openai": {
        "mapping_model": "sonnet",
        "OPENAI_API_KEY": "sk-...",
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "upstream_model": "gpt-5-mini"
    },
    "claude-haiku-4-x-via-openai": {
        "mapping_model": "haiku",
        "OPENAI_API_KEY": "sk-...",
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "upstream_model": "gpt-4o-mini"
    },
    "claude-fable-5-x-via-openai": {
        "mapping_model": "fable",
        "OPENAI_API_KEY": "sk-...",
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "upstream_model": "gpt-4o-mini"
    }
}
```

约定：每个模型 entry 可选 `mapping_model: "opus" | "sonnet" | "haiku" | "fable"`，用于显式指定写入 Claude CLI 的哪个家族槽位。未配置 `mapping_model` 的槽位继续按旧规则 fallback：4 个 key 分别按顺序对应 **opus / sonnet / haiku / fable**。
启动时会自动写入 `~/.claude/settings.json`：

```json
{
  "model": "opus",
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "openai2claude-local-token",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8080",
    "ANTHROPIC_MODEL": "claude-opus-4-x-via-openai",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-x-via-openai",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-x-via-openai",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-x-via-openai",
    "ANTHROPIC_DEFAULT_FABLE_MODEL": "claude-fable-5-x-via-openai",
    "ANTHROPIC_SMALL_FAST_MODEL": "claude-haiku-4-x-via-openai",
    "CLAUDE_CODE_EFFORT_LEVEL": "max"
  }
}
```

如果上游不是 OpenAI 官方而是兼容服务（DeepSeek / 通义 / Together / 本地 vllm 等），
只要它实现了 `/v1/chat/completions`，就把 `OPENAI_BASE_URL` 改成它的根地址即可。

### 3.1 `provider` 与 `model_series` 字段

每个 entry 可选 `mapping_model` 声明 Claude Code 家族槽位，例如 `opus` / `sonnet` / `haiku` / `fable`。它只影响写入 `~/.claude/settings.json` 的 `ANTHROPIC_DEFAULT_*_MODEL`，不影响 OpenAI 上游请求。

每个 entry 用 `provider` 声明该上游的**服务商**，例如 `openai` / `deepseek` / `glm`。未填或填了不认识的值 → 按 `openai`（标准 chat completions）处理。

模型族/版本带来的参数差异用可选的 `model_series`（或别名 `model_code`）声明，例如 `gpt-5.5` / `gpt-5` / `o3` / `reasoning` / `deepseek-v4-pro` / `kimi-k2-thinking`。

| 字段 | 取值 | 行为 |
| --- | --- | --- |
| `provider` | `openai`（默认） | OpenAI 官方或 OpenAI 兼容网关，默认标准 chat 参数：`max_tokens` + `temperature` + `top_p` + `stop`。 |
| `provider` | `deepseek` | DeepSeek 服务商，标准 chat 参数。 |
| `provider` | `kimi` / `moonshot` | Moonshot Kimi 服务商，标准 chat 参数。 |
| `provider` | `qwen` / `dashscope` | 阿里通义千问 OpenAI 兼容入口，标准 chat 参数。 |
| `provider` | `zhipu` / `glm` | 智谱 GLM，标准 chat 参数。 |
| `provider` | `minimax` / `together` / `groq` / `ollama` / `vllm` / `generic` | 通用 OpenAI 兼容服务，标准 chat 参数。 |
| `model_series` | `gpt-5.5` / `gpt-5` / `o1` / `o3` / `o4` / `reasoning` | OpenAI 风格 reasoning 模型：用 `max_completion_tokens`，不发 `temperature/top_p/stop`，**自动把 `delta.reasoning_content` 透传为 Anthropic `thinking` block**。 |
| `model_series` | `deepseek-reasoner` / `deepseek-v3` / `deepseek-v4` / `deepseek-v4-pro` / `deepseek-r1` / `glm-4.6` / `glm-4.6-thinking` / `glm-zero` / `kimi-k2` / `kimi-k2-thinking` / `thinking` | 标准 chat 协议 + 暴露 reasoning 通道：`max_tokens` + `temperature/top_p/stop` 正常发，**额外把 `delta.reasoning_content` 透传为 `thinking` block**。 |

⚠️ **重要**：`provider` 不再承载模型族含义。如果上游服务商是 OpenAI，但模型是 `gpt-5.5` / `gpt-5` / `o3` 这类 reasoning 模型，请写：`"provider": "openai"` + `"model_series": "gpt-5.5"`。

#### 3.1.1 为什么要声明 `model_series`：reasoning 通道转换

deepseek-v4-pro / o-series / glm-thinking / kimi-k2-thinking 等模型在 OpenAI 兼容流里，**推理过程走 `delta.reasoning_content`，最终答案才走 `delta.content`**。当任务足够"简单"时，模型可能把整个 output 预算都用在 reasoning 通道，`content` 一个 token 都没有。

如果 proxy 对此不感知（按"openai 标准 chat"翻译），Anthropic SSE 流里就既无 `text` block 也无 `tool_use` block，Claude Code 直接报：

```
Error: No assistant messages found
```

正确的 `model_series` 声明会让 proxy 把 `reasoning_content` 翻译成 Anthropic 原生的 `thinking` content block，于是：

1. ✅ 即使 `content` 为空，下游 SDK 也能拿到非空 assistant 消息，不会报上面那个错。
2. ✅ Claude Code UI 会渲染出可折叠的 thinking 区块，用户能看到完整推理过程。
3. ✅ usage / `reasoning_tokens` 仍然透传。

每次转发都会在 `proxy.log` 打印 `provider=... model_series=... max_field=...`；流结束时还会打印 `reasoning_bytes=... reasoning_mode=...`，方便排查 reasoning 是否被正确捕获。未识别的 provider/model_series 名会以 `(unknown→standard)` / `(unknown)` 标注。

#### 3.1.2 常见模型配置速查

> 关键判断：**模型在 OpenAI 兼容协议下是否会输出 `delta.reasoning_content`** 决定是否需要 `model_series`。

##### OpenAI 官方 / Azure OpenAI

| 模型 | `provider` | `model_series` |
| --- | --- | --- |
| gpt-4o / gpt-4o-mini / gpt-4-turbo / gpt-3.5-turbo | `openai` | *(不填)* |
| gpt-5 / gpt-5-mini / gpt-5.5 | `openai` | `gpt-5`（或 `gpt-5.5`） |
| o1 / o1-mini / o1-preview | `openai` | `o1` |
| o3 / o3-mini | `openai` | `o3` |
| o4 / o4-mini | `openai` | `o4` |

```json
{
  "claude-opus-4-x-via-openai-gpt5": {
    "mapping_model": "opus",
    "provider": "openai",
    "model_series": "gpt-5",
    "OPENAI_API_KEY": "sk-...",
    "OPENAI_BASE_URL": "https://api.openai.com/v1",
    "upstream_model": "gpt-5"
  }
}
```

##### DeepSeek

| 模型 | `provider` | `model_series` |
| --- | --- | --- |
| deepseek-chat（V3 chat） | `deepseek` | *(不填)* |
| deepseek-coder | `deepseek` | *(不填)* |
| deepseek-reasoner / deepseek-r1 | `deepseek` | `deepseek-reasoner`（或 `deepseek-r1`） |
| deepseek-v3-reasoner | `deepseek` | `deepseek-v3` |
| deepseek-v4-pro | `deepseek` | `deepseek-v4-pro` |

```json
{
  "claude-haiku-4-x-via-deepseek-r1": {
    "mapping_model": "haiku",
    "provider": "deepseek",
    "model_series": "deepseek-reasoner",
    "OPENAI_API_KEY": "sk-...",
    "OPENAI_BASE_URL": "https://api.deepseek.com/v1",
    "upstream_model": "deepseek-reasoner"
  }
}
```

##### 智谱 GLM (BigModel / open.bigmodel.cn)

| 模型 | `provider` | `model_series` |
| --- | --- | --- |
| glm-4 / glm-4-plus / glm-4-air / glm-4-flash | `glm` | *(不填)* |
| glm-4.6 (thinking) | `glm` | `glm-4.6` |
| glm-zero / glm-zero-preview | `glm` | `glm-zero` |

```json
{
  "claude-opus-4-x-via-glm46": {
    "mapping_model": "opus",
    "provider": "glm",
    "model_series": "glm-4.6",
    "OPENAI_API_KEY": "...",
    "OPENAI_BASE_URL": "https://open.bigmodel.cn/api/paas/v4",
    "upstream_model": "glm-4.6"
  }
}
```

##### Moonshot Kimi (kimi.moonshot.cn / api.moonshot.cn)

| 模型 | `provider` | `model_series` |
| --- | --- | --- |
| moonshot-v1-8k / 32k / 128k | `kimi` | *(不填)* |
| kimi-k2 / kimi-k2-instruct | `kimi` | *(不填)* 或 `kimi-k2` |
| kimi-k2-thinking | `kimi` | `kimi-k2-thinking` |

```json
{
  "claude-opus-4-x-via-kimi-thinking": {
    "mapping_model": "opus",
    "provider": "kimi",
    "model_series": "kimi-k2-thinking",
    "OPENAI_API_KEY": "sk-...",
    "OPENAI_BASE_URL": "https://api.moonshot.cn/v1",
    "upstream_model": "kimi-k2-thinking"
  }
}
```

##### 阿里通义千问 / DashScope (OpenAI 兼容入口)

| 模型 | `provider` | `model_series` |
| --- | --- | --- |
| qwen-max / qwen-plus / qwen-turbo | `qwen` | *(不填)* |
| qwen2.5-* / qwen3-* | `qwen` | *(不填)* |
| qwen-thinking / qwq-* | `qwen` | `thinking` |

##### 其他 OpenAI 兼容服务

| 服务 | `provider` |
| --- | --- |
| MiniMax / Together AI / Groq / Fireworks | `minimax` / `together` / `groq` / `generic` |
| 自部署 vllm / ollama / sglang | `vllm` / `ollama` / `generic` |
| 内部 OpenAI 网关（如快手 wanqing-api） | `openai`（再叠加正确的 `model_series`） |

##### 公司内网网关示例（快手 wanqing-api）

```json
{
  "claude-opus-4-gpt5-5": {
    "mapping_model": "opus",
    "provider": "openai",
    "model_series": "gpt-5.5",
    "OPENAI_API_KEY": "...",
    "OPENAI_BASE_URL": "https://wanqing-api.corp.kuaishou.com/api/gateway/v1/endpoints",
    "upstream_model": "ep-g2b2hp-1778577740786242030"
  },
  "claude-haiku-4-deepseek-v4-pro": {
    "mapping_model": "haiku",
    "provider": "deepseek",
    "model_series": "deepseek-v4-pro",
    "OPENAI_API_KEY": "...",
    "OPENAI_BASE_URL": "https://wanqing-api.corp.kuaishou.com/api/gateway/v1",
    "upstream_model": "ep-vxd5bq-1778244794659259230"
  }
}
```

> 拿不准时的简单决策：**模型名里带 `thinking` / `reasoner` / `r1` / `o1`-`o4` / `gpt-5` 关键词的，几乎都需要声明 `model_series`**。漏配会触发 `proxy.log` 里的 `STREAM_CONVERT empty_visible_output` 警告，以及下游 `Error: No assistant messages found`。

## 4. 启动 / 停止 / 状态

```bash
./bin/start.sh                       # 默认 0.0.0.0:8080，config.json，log/
./bin/start.sh --port 8091
./bin/start.sh --config /path/to/config.json --log-dir /var/log/openai2claude

./bin/status.sh                      # 进程、端口、log 摘要、HTTP /v1/models 探活
./bin/status.sh --tail 50            # 顺便贴近期日志

./bin/stop.sh                        # 先 SIGTERM，10 秒不退则 SIGKILL
./bin/stop.sh --all --force          # 清理孤儿进程

./bin/restart.sh                     # = stop + start（参数透传给两者）
./bin/reload_config.sh               # 调 /admin/reload，无需重启进程
```

> ⚠️ 默认端口 **8080**；该端口与 `../../claude_proxy/` 默认端口相同，两者并行运行请用 `--port` 错开。

### 4.1 进程识别策略（pid 文件丢失也能找回进程）

`start.sh / stop.sh / status.sh / restart.sh` 共享 `bin/_proc_lib.sh`，按下列顺序定位运行实例：

1. **PID 文件**：`log/proxy.pid` 存在且其中 PID 进程命令行匹配 `proxy.py` → 直接使用。
2. **ps 兜底扫描**：PID 文件缺失 / 内容为空 / PID 已死 / PID 还活但已不是 proxy 时，自动 `ps -axo pid=,user=,command=` 同用户进程并经过两段过滤：
   - cmdline 必须含 `python` 且含 `proxy.py`；
   - **再用 `lsof -p <pid> -d cwd` 验证进程的工作目录等于本项目根**——这样既能识别 `cd <project> && python3 proxy.py` 这种相对路径启动方式，也不会误杀系统上无关的同名脚本。
3. **绝对路径强匹配**：cmdline 直接含 `<project>/proxy.py` 也算命中（不需要 cwd 检查）。

效果：**不再因为 pid 文件丢失就以为服务没起；也不会误杀别的进程。** stop / status 会清晰打印 `detected via: pid-file` 或 `Recovered via ps scan: PID(s) ...`。

### 4.2 启动参数

| 参数 | 环境变量 | 默认 | 说明 |
| --- | --- | --- | --- |
| `--port` | `PROXY_PORT` | `8080` | 监听端口 |
| `--config` | `PROXY_CONFIG` | `config.json` | 配置文件路径 |
| `--log-dir` | `PROXY_LOG_DIR` | `<project>/log` | 日志目录 |
| `--http-timeout` | `PROXY_HTTP_TIMEOUT` | `600` | 非流式 socket 超时（秒） |
| `--stream-timeout` | `PROXY_STREAM_TIMEOUT` | `600` | 流式空闲超时（秒） |
| `--stream-keepalive` | `PROXY_STREAM_KEEPALIVE` | `10` | SSE keep-alive ping 间隔（秒） |
| `--max-retries` | `PROXY_MAX_RETRIES` | `2` | 上游 5xx / 网络层重试次数 |
| `--upstream-idle-limit` | `PROXY_UPSTREAM_IDLE_LIMIT` | `0` | 上游主动 idle 关闭秒数（0=禁用） |
| `--empty-stop-max-retry` | `PROXY_EMPTY_STOP_MAX_RETRY` | `2` | 空 finish=stop 兜底重试次数（0=禁用） |

## 5. 在 Claude Code 中使用

启动后，`~/.claude/settings.json` 已经自动指向本代理。打开新的 `claude` 终端：

```bash
claude
> /model            # 应能看到上面 4 个 claude-*-via-openai 槽位
> 你好，自我介绍下你是谁
```

请求会沿着：

```
claude → http://127.0.0.1:8080/v1/messages → openai2claude → https://api.openai.com/v1/chat/completions
```

回包则被翻译回 Anthropic SSE，CLI 会以为它在和 Claude 对话。

## 6. 端点

| 端点                          | 说明                                                           |
| ----------------------------- | -------------------------------------------------------------- |
| `GET  /v1/models`             | 返回 `config.json` 里的 model id 列表（Anthropic 形态）        |
| `POST /v1/messages`           | 主入口。流式 / 非流式都支持，并完成 OpenAI ↔ Anthropic 翻译     |
| `POST /v1/messages/count_tokens` | 启发式实现（`chars/4`）—— 上游协议不通用，避免引入额外依赖   |
| `GET  /admin/health`          | 简易健康检查                                                   |
| `POST/GET /admin/reload`      | 热加载 `config.json` 并同步 `~/.claude/settings.json`          |

## 6.1 流可靠性增强（Anthropic 行为对齐）

针对上游网关偶发的"假装答完了"和长 thinking 期心跳问题，proxy 在 SSE 流层做了两件 Anthropic 官方网关的标准动作：

### 空 `finish=stop` 兜底重试

某些上游（典型如 deepseek-v4-pro 在 prompt 接近上下文上限时）会返回 `finish_reason="stop"` + `completion_tokens=1` + 无 text / 无 reasoning / 无 tool_call 的"空 turn"。如果原样转发给 Claude Code，它会按 `end_turn` 处理并进入很长的客户端退避（实测 170+ 秒）。

proxy 现在会**在同一条 SSE 连接里**，对这种空 turn 静默重发上游请求：

- 命中条件（必须全部成立）：`finish_reason=stop` ∧ 无 `text_delta` ∧ 无 `reasoning_content` ∧ 无 `tool_call` ∧ `completion_tokens<=1`。
- 默认最多重试 `2` 次（即总共 1+2=3 次上游调用），可经 `--empty-stop-max-retry` / `PROXY_EMPTY_STOP_MAX_RETRY` 调整，`0` 关闭。
- 重试间退避 `0.5s → 1s`；retry 上游若返回 4xx/5xx，立刻终止退避并按错误结束流。
- 重用同一个 `message_start` 与 content block index 序列——客户端只看到一个 Anthropic message，没有重复 message_start。
- 触发时 `proxy.log` 会写：

  ```
  STREAM_CONVERT empty_stop_retry request_id=o2c_xxx attempt=1/3 completion_tokens=1 — re-issuing upstream request
  ```

### Anthropic 风格 `event: ping` 心跳

旧实现使用 SSE 注释（`: keep-alive\n\n`）维持长 thinking 期连接，但 httpx / anthropic-sdk-python 等严格 SDK 会忽略注释、把它当成 idle 期。改为 Anthropic 官方协议形式后，所有合规 SDK 都会重置 idle 计时：

```
event: ping
data: {"type":"ping"}
```

默认间隔由 20s 缩短为 **10s**，可通过 `--stream-keepalive` 调整。

## 7. 日志

与 `claude_proxy` 完全一致：

| 文件                        | 内容                                                       |
| --------------------------- | ---------------------------------------------------------- |
| `log/proxy.log`             | 结构化业务日志（`logger.*`，RotatingFileHandler 100MB×6）  |
| `log/proxy.log.{1..6}`      | 滚动归档                                                   |
| `log/proxy.out`             | `print(...)` 与子进程 stdout                                |
| `log/proxy.err`             | `print(file=sys.stderr)` 与异常 traceback                   |
| `log/proxy.pid`             | 当前守护进程 PID                                            |

`bin/status.sh` 会列出这一切。

## 8. 转换层设计原则

`openai2claude` 不做零散字段补丁，而是统一走标准对象转换边界：

```text
Claude raw request
  → ClaudeMessagesRequest
  → ClaudeOpenAIConverter
  → OpenAIChatRequest
  → OpenAI /chat/completions
  → OpenAIChatCompletion
  → ClaudeOpenAIConverter
  → ClaudeMessagesResponse / Anthropic SSE
```

核心对象：

| 标准对象 | 作用 |
| --- | --- |
| `ClaudeMessagesRequest` | 标准化 Claude `/v1/messages` 请求，包括 `messages/system/tools/tool_choice/max_tokens/stream`。 |
| `OpenAIChatRequest` | 标准化 OpenAI Chat Completions 请求，并通过 `to_openai_dict()` 生成最终上游 JSON。 |
| `OpenAIChatCompletion` | 标准化完整 OpenAI 响应；非流式直接由上游 JSON 创建，流式先由 `OpenAIStreamAccumulator` 聚合后创建。 |
| `ClaudeMessagesResponse` | 标准化 Claude Messages 响应，并通过 `to_anthropic_dict()` 输出给 Claude Code。 |
| `ClaudeOpenAIConverter` | 唯一转换接口，负责 Claude ↔ OpenAI 对象转换。 |

这样非流式和流式响应最终共用同一条链路：

```text
OpenAIChatCompletion → ClaudeOpenAIConverter → ClaudeMessagesResponse / content blocks
```

如果未来要支持更多协议差异，应优先扩展这些标准对象和 `ClaudeOpenAIConverter`，不要在 HTTP handler 或 SSE 循环里直接拼字段。

## 9. 已知限制

1. `count_tokens` 用启发式（4 字节/Token）。如果你需要精确 token 数，请把上游切换到一个真正能反查 tokenizer 的服务。
2. 仅支持 `text` / `tool_use` / `tool_result` 三种 content block。`image` / `document` block 暂未翻译（OpenAI 视觉接口要求图片走多模态字段，差异较大）。
3. OpenAI 的 `function_call` 旧协议**不**翻译，只支持 `tool_calls`（OpenAI 自 2024 起的新协议）。
4. 上游必须是 **OpenAI 兼容** 的 `/v1/chat/completions`。Responses API（`/v1/responses`）目前不支持。

## 10. 与 `claude_proxy` 的关系

| 维度                 | `claude_proxy`                      | `openai2claude`（本目录）          |
| -------------------- | ----------------------------------- | ---------------------------------- |
| 入口协议             | Anthropic                           | Anthropic                          |
| 出口协议             | Anthropic（透传）                   | **OpenAI Chat Completions**        |
| 工作内容             | 多上游路由 + 透传                   | **协议翻译**                       |
| 默认端口             | 8080                                | 8080（与 claude_proxy 相同，并行时需手工错开）  |
| 配置形态             | 4 槽位 × `ANTHROPIC_*`              | 4 槽位 × `OPENAI_*` + `upstream_model` |
| 运维脚本 / 日志策略  | 一致                                | 一致                                |

两者可以**并行**跑，但需手动错开端口（如 `claude_proxy` 保持 8080，本服务启动时 `--port 8090`），然后通过修改 `~/.claude/settings.json` 的 `ANTHROPIC_BASE_URL` 在两者间切换。
