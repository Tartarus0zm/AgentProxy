# Claude Code API Proxy

一个**轻量级 Python 代理服务**，让 [Claude Code CLI](https://docs.claude.com/) 可以无缝接入任何兼容 Anthropic API 协议的上游网关（例如内网 LLM 网关、自建模型服务、其他云厂商兼容层等）。

主要能力：

- **多模型路由**：根据请求 `model` 字段自动转发到不同上游 endpoint
- **SSE 流式透传**：完整支持 `stream: true` 的逐 token 输出，自动处理 `Accept-Encoding` 避免 gzip 缓冲卡死
- **SDK 风格容错**：网络错误 / 408 / 409 / 429 / 5xx 自动重试（仅在流开始前），指数退避 + 抖动 + 尊重 `Retry-After`
- **流式透明透传**：默认不注入任何 SSE 内容；可显式开启 `--stream-keepalive` 做保活
- **热加载配置**：通过 `/admin/reload` 接口或 `bin/reload_config.sh` 脚本动态生效
- **自动同步 `~/.claude/settings.json`**：根据 `config.json` 自动写入 Claude CLI 需要的环境变量
- **滚动日志**：Python `RotatingFileHandler`，默认 6 个文件 × 100MB
- **完整运维脚本**：`start.sh` / `stop.sh` / `restart.sh` / `status.sh` / `reload_config.sh`

---

## 目录结构

```
claude_proxy/
├── proxy.py                  # 代理主程序（单文件，无第三方依赖）
├── config.json               # 你的实际配置（git 忽略；从 template 复制）
├── config.json.template      # 配置模板（脱敏，可入库）
├── bin/
│   ├── start.sh              # 启动（nohup 后台 + 严格预检）
│   ├── stop.sh               # 优雅停止（pid + ps 双重定位）
│   ├── restart.sh            # 重启（stop + start 一体）
│   ├── status.sh             # 查看运行状态（进程/端口/日志/HTTP 探测）
│   └── reload_config.sh      # 触发 /admin/reload 热加载
├── log/                      # 日志目录（自动创建）
│   ├── proxy.log             # 业务日志（滚动）
│   ├── proxy.log.1 ~ .6      # 滚动归档
│   ├── proxy.out             # 进程 stdout（仅 print() 输出）
│   ├── proxy.err             # 进程 stderr（仅未捕获异常）
│   └── proxy.pid             # 当前进程 PID 文件
├── README.md
└── PLAN.md                   # 设计说明
```

---

## 快速开始

### 1. 准备配置

```bash
cp config.json.template config.json
vim config.json
```

填入你的上游 `ANTHROPIC_AUTH_TOKEN` 和 `ANTHROPIC_BASE_URL`（详见下方 [config.json 配置规范](#configjson-配置规范)）。

### 2. 启动代理

```bash
./bin/start.sh
```

默认监听 `0.0.0.0:8080`，使用项目目录下的 `config.json`。

启动成功后会自动：

1. 写入 PID 文件 `log/proxy.pid`
2. 同步 `~/.claude/settings.json`，把配置中的模型注入 `ANTHROPIC_DEFAULT_OPUS_MODEL` / `SONNET_MODEL` / `HAIKU_MODEL` / `FABLE_MODEL` 四个槽位；优先使用每个模型条目的 `mapping_model`，未显式配置的槽位继续按 JSON 顺序 fallback
3. 设置 `ANTHROPIC_BASE_URL = http://<本机>:8080`，让 Claude CLI 走代理

### 3. 配置 Claude CLI 的环境变量

在启动 `claude` 之前，需要让它把请求发到本地代理。设置两个环境变量：

```bash
# 指向本机运行的代理（端口与 start.sh 一致；127.0.0.1 / localhost 均可）
export ANTHROPIC_BASE_URL="http://127.0.0.1:8080"

# 这个值随便填即可。真正生效的 token 是 config.json 里的 ANTHROPIC_AUTH_TOKEN，
# 由代理在转发时注入；CLI 这里设置的 token 不会被透传到上游。
export ANTHROPIC_AUTH_TOKEN="placeholder-not-used"
```

**临时生效**（仅当前终端）：直接执行上面两行 `export` 即可。

**永久生效**：把上面两行追加到你的 shell 配置文件，然后用 `source` 重新加载：

```bash
# zsh 用户（macOS 默认）
echo 'export ANTHROPIC_BASE_URL="http://127.0.0.1:8080"' >> ~/.zshrc
echo 'export ANTHROPIC_AUTH_TOKEN="placeholder-not-used"' >> ~/.zshrc
source ~/.zshrc

# bash 用户
echo 'export ANTHROPIC_BASE_URL="http://127.0.0.1:8080"' >> ~/.bash_profile
echo 'export ANTHROPIC_AUTH_TOKEN="placeholder-not-used"' >> ~/.bash_profile
source ~/.bash_profile
```

> 验证：`echo $ANTHROPIC_BASE_URL` 应输出 `http://127.0.0.1:8080`。

> 端口不是 8080 时记得替换；如果代理只想本机访问，启动时可加 `--host 127.0.0.1`，此处环境变量也用 `127.0.0.1`。

### 4. 启动 Claude CLI

```bash
claude
```

在 CLI 里执行 `/model` 即可看到各模型槽位映射的实际模型名称。

---

## config.json 配置规范

### 关键约定

| 约定 | 说明 |
|---|---|
| **建议 3~4 个模型** | claude-code v2.1.172+ 在原有 OPUS / SONNET / HAIKU 之外新增了 **FABLE** 槽位（Fable 5 档位），共支持 4 个家族槽位。配置 ≤4 项，缺失的槽位会回退到 config 中**最后一个**模型。 |
| **显式映射槽位** | 每个模型条目可选 `"mapping_model": "opus" | "sonnet" | "haiku" | "fable"`。配置后优先写入对应的 `ANTHROPIC_DEFAULT_*_MODEL`。 |
| **兼容旧顺序映射** | 没有配置 `mapping_model` 的槽位继续按旧规则 fallback：第 **1** 个 → `OPUS`；第 **2** 个 → `SONNET`；第 **3** 个 → `HAIKU`；第 **4** 个 → `FABLE`。 |
| **默认模型** | 配置文件中**第 1 个**模型 ID 会写入 `ANTHROPIC_MODEL`（即 CLI 启动后的默认模型） |
| **模型 ID 命名** | 推荐 `claude-{family}-{version}-{date}`（如 `claude-opus-4-8-20260101`），不影响功能但便于在菜单中识别 |
| **token / URL 含义** | `ANTHROPIC_AUTH_TOKEN` 是上游网关 token；`ANTHROPIC_BASE_URL` 是上游 endpoint **完整 URL**（可以带路径） |

### 模板（`config.json.template`）

```json
{
    "claude-opus-4-x-YYYYMMDD": {
        "mapping_model": "opus",
        "ANTHROPIC_AUTH_TOKEN": "YOUR_UPSTREAM_TOKEN_HERE",
        "ANTHROPIC_BASE_URL": "https://your-upstream-host.example.com/path/to/opus-endpoint"
    },
    "claude-sonnet-4-x-YYYYMMDD": {
        "mapping_model": "sonnet",
        "ANTHROPIC_AUTH_TOKEN": "YOUR_UPSTREAM_TOKEN_HERE",
        "ANTHROPIC_BASE_URL": "https://your-upstream-host.example.com/path/to/sonnet-endpoint"
    },
    "claude-haiku-4-x-YYYYMMDD": {
        "mapping_model": "haiku",
        "ANTHROPIC_AUTH_TOKEN": "YOUR_UPSTREAM_TOKEN_HERE",
        "ANTHROPIC_BASE_URL": "https://your-upstream-host.example.com/path/to/haiku-endpoint"
    },
    "claude-fable-5-x-YYYYMMDD": {
        "mapping_model": "fable",
        "ANTHROPIC_AUTH_TOKEN": "YOUR_UPSTREAM_TOKEN_HERE",
        "ANTHROPIC_BASE_URL": "https://your-upstream-host.example.com/path/to/fable-endpoint"
    }
}
```

> **FABLE 槽位说明**：claude-code v2.1.172+ 在二进制中已经内置 `ANTHROPIC_DEFAULT_FABLE_MODEL` 及其同族 `_NAME` / `_DESCRIPTION` / `_SUPPORTED_CAPABILITIES` 变量，对应所谓的 "Fable 5" 档位，与 OPUS 4.8 配合做 fallback 路由。如果你只有 3 个上游 endpoint，可以省略第 4 项，proxy 会让 fable 槽位自动回退到 config 中最后一个模型。

### 字段说明

- **顶层 key**（如 `claude-opus-4-x-YYYYMMDD`）就是该模型的**对外 ID**，Claude CLI 在 `/v1/models` 中看到的就是它。
- **`ANTHROPIC_AUTH_TOKEN`**：上游网关认证 token；代理会在转发请求时同时设置 `x-api-key` 和 `Authorization: Bearer <token>` 两个头。
- **`ANTHROPIC_BASE_URL`**：上游完整 URL，**包含路径**。例如内网网关常见形式：  
  `https://gateway.example.com/api/gateway/v1/endpoints/ep-xxxxx/claude-code-proxy`  
  注意：**模型 ID 通常已经编码在 URL 路径中**（如 `ep-xxxxx`），所以请求 body 里的 `model` 字段对真正路由并不重要。
- **`mapping_model`**：可选，取值为 `opus` / `sonnet` / `haiku` / `fable`，用于显式指定该模型写入哪个 Claude Code 家族槽位。未配置或某个槽位没有显式配置时，继续使用旧的 JSON 顺序 fallback 规则。
- **`_comment`**：JSON 不支持注释，使用 `_comment` 字段做行内说明会被代理忽略（不报错）。

### 热加载

修改 `config.json` 后无需重启进程：

```bash
./bin/reload_config.sh
```

会触发 `/admin/reload`，同时自动同步 `~/.claude/settings.json`。

---

## 运维脚本

### `bin/start.sh` — 启动

```bash
./bin/start.sh [--port PORT] [--config CONFIG] [--log-dir DIR]
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-p, --port` | `8080` | 监听端口 |
| `-c, --config` | `config.json`（相对项目根目录） | 配置文件路径 |
| `-l, --log-dir` | `<project>/log` | 日志目录 |

特性：
- 使用 `nohup` 后台启动
- **严格预检**：先看 pid 文件，再用 `ps` 扫描当前用户的 `proxy.py` 进程；端口被占用也会拒绝启动
- 只有启动成功才写入 pid 文件

### `bin/stop.sh` — 停止

```bash
./bin/stop.sh [--log-dir DIR] [--timeout SECS] [--force] [--all]
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-t, --timeout` | `10` | SIGTERM 后等待优雅退出的秒数；超时升级为 SIGKILL |
| `-f, --force` | - | 跳过 SIGTERM 直接 SIGKILL |
| `-a, --all` | - | 检测到多个 proxy 进程时全部停止 |

### `bin/restart.sh` — 重启

```bash
./bin/restart.sh [start.sh 参数 + stop.sh 参数]
```

兼容 start 和 stop 的全部参数。

### `bin/status.sh` — 状态查看

```bash
./bin/status.sh [--port PORT] [--no-http] [--tail N]
```

输出：进程 PID / 用户 / 状态 / 运行时长 / CPU+内存占用 / 命令行 / 端口监听情况 / 日志文件大小 / `/v1/models` HTTP 探测 / 最近 N 行日志。

退出码：
- `0` — 进程在线且 HTTP 健康
- `1` — 进程在线但 HTTP 异常
- `2` — 进程未运行

### `bin/reload_config.sh` — 热加载

```bash
./bin/reload_config.sh [--port PORT] [--host HOST]
```

调用 `GET /admin/reload`，重新读取 `config.json` 并同步 `~/.claude/settings.json`。

---

## 直接运行（不使用脚本）

```bash
python3 proxy.py \
  --port 8080 \
  --config config.json \
  --log-dir ./log \
  --log-max-bytes 104857600 \
  --log-backup-count 6
```

| 参数 | 环境变量 | 默认值 |
|---|---|---|
| `--port` | `PROXY_PORT` | `8080` |
| `--config` | `PROXY_CONFIG` | `config.json` |
| `--host` | - | `0.0.0.0` |
| `--log-dir` | `PROXY_LOG_DIR` | `<project>/log` |
| `--log-max-bytes` | `PROXY_LOG_MAX_BYTES` | `104857600`（100 MB） |
| `--log-backup-count` | `PROXY_LOG_BACKUP_COUNT` | `6` |
| `--http-timeout` | `PROXY_HTTP_TIMEOUT` | `600`（秒，非流式 socket 超时） |
| `--stream-timeout` | `PROXY_STREAM_TIMEOUT` | `600`（秒，流式真·空闲上限） |
| `--stream-keepalive` | `PROXY_STREAM_KEEPALIVE` | `0`（秒，0 = 禁用；默认保持 SSE 响应透明） |
| `--upstream-idle-limit` | `PROXY_UPSTREAM_IDLE_LIMIT` | `0`（秒，0 = 禁用；>0 时上游静默达此秒数主动关流并写错误事件） |
| `--max-retries` | `PROXY_MAX_RETRIES` | `2`（仅流开始前重试） |

---

## 日志策略

| 文件 | 内容 | 滚动 |
|---|---|---|
| `log/proxy.log` | **所有** `logger.*` 输出（业务日志） | ✅ 100MB × 6 |
| `log/proxy.out` | 仅 `print(...)` 的 stdout 输出 | ❌（每次 start 清空） |
| `log/proxy.err` | 仅未捕获异常 / Python 自身错误 | ❌（每次 start 清空） |

**健康标志**：`proxy.err` 长期为空 = 没有异常。

实时查看：
```bash
tail -f log/proxy.log
```

### 流式日志要点

每次流式请求结束都会输出一条 `Stream finished` 汇总日志：

```
Stream finished: lines=247 bytes=18432 keepalives=0 idle_at_close=0.04s client_gone=False
```

| 字段 | 含义 | 健康值 |
|---|---|---|
| `lines / bytes` | 实际转发的 SSE 行数 / 字节数 | 正常完整响应通常几十 KB |
| `keepalives` | 代理注入了几次 `:keep-alive` 注释 | 多数情况应为 `0` |
| `idle_at_close` | 关闭时距离最后一次有效数据的秒数 | 正常完成时应 `< 1s` |
| `client_gone` | 是否客户端先断开 | `False` 表示上游或代理结束流；`True` 表示 CLI 主动取消 |

---

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/models` | 列出 config.json 中的模型 |
| POST | `/v1/messages` | 发送消息（支持 SSE 流式） |
| POST | `/v1/messages/count_tokens` | 计算 token |
| POST | `/v1/messages/batches` | 批量请求 |
| GET | `/v1/messages/batches/{id}` | 批量状态 |
| GET | `/v1/messages/batches/{id}/results` | 批量结果 |
| POST | `/v1/files` | 上传文件 |
| GET | `/v1/files` | 文件列表 |
| GET | `/v1/files/{id}` | 文件详情 |
| DELETE | `/v1/files/{id}` | 删除文件 |
| GET/POST | `/admin/reload` | 热加载配置 |

---

## 常见问题

### Q: `/model` 菜单只显示 Default/Sonnet/Sonnet 1M/Haiku，没有我配置的模型？

A: Claude CLI 的 `/model` UI 是**硬编码的家族选择菜单**，不会从 `/v1/models` 动态加载。代理在启动时会根据 `mapping_model`（或旧的 JSON 顺序 fallback）把你的模型注入到 `ANTHROPIC_DEFAULT_OPUS/SONNET/HAIKU/FABLE_MODEL` 槽位，菜单里选哪个槽位就走对应的上游 endpoint。

### Q: 什么是 FABLE 槽位？我需要配吗？

A: claude-code v2.1.172+ 新增的第 4 个家族档位（Fable 5），与 OPUS 4.8 配合做自动 fallback。proxy 支持通过 `mapping_model: "fable"` 显式注入 `ANTHROPIC_DEFAULT_FABLE_MODEL`；未配置时继续使用旧规则回退到第 4 个/最后一个模型。

- 如果你有独立的 Fable endpoint：在对应模型条目里配置 `"mapping_model": "fable"` 即可
- 如果暂时没有：不配置 `mapping_model: "fable"` 也可以，proxy 会让 fable 按旧顺序规则自动回退到第 4 个/最后一个模型，不会报错

### Q: 上游返回 `429 TooManyRequests`？

A: 这是上游网关的 token 速率限制。可选：
- 在 `/model` 切换到另一个槽位（不同 endpoint 配额独立）
- 联系上游平台提升 rate limit
- 在 `~/.claude/settings.json` 把 `CLAUDE_CODE_EFFORT_LEVEL` 从 `max` 降到 `medium` 或 `low`，减少 token 消耗

### Q: 代理拒绝启动，提示 "proxy already running"？

A: `start.sh` 做了严格预检，会检测 pid 文件 + `ps` 全局扫描。如果确实是僵尸进程，可用：
```bash
./bin/stop.sh --force   # 强制 kill
./bin/start.sh
```

### Q: 想完全自定义启动方式（systemd / supervisor 等）？

A: 直接调用 `python3 proxy.py --port ... --config ... --log-dir ...` 即可，所有参数都支持环境变量覆盖。

### Q: SSE 流式响应卡住、几十秒后断开 / 输出截断？

A: 几乎都是 **`Accept-Encoding: gzip` 透传** 引起的"流式 + gzip 缓冲"经典坑。代理已经在转发时强制把 `Accept-Encoding` 改为 `identity`，避免上游网关启用 gzip 后因不调用 `Z_SYNC_FLUSH` 而导致流缓冲卡死。如果你**自定义改动了 `forward_request()`** 又还原了这个 header，会再次踩坑。

诊断方法：
1. 用 `curl -N`（不带 `Accept-Encoding` 头）直连上游 endpoint，看是否能完整流式输出
2. 查看 `log/proxy.log` 的 `Stream finished: ... idle_at_close=...` 这一行
   - `idle_at_close < 1s` + `keepalives=0` → 完全正常
   - `idle_at_close > 30s` 且 `client_gone=False` → 上游真静默或代理逻辑异常
   - 出现 `Connection reset by peer` 但 curl 直连正常 → 代理 bug，先检查 `Accept-Encoding`

### Q: 长 thinking 任务超时？怎么调大超时？

A: 默认 `--stream-timeout=600` 已经较宽松。如果上游 thinking 真的会超过 10 分钟，可调大：

```bash
export PROXY_STREAM_TIMEOUT=1200    # 20 分钟
./bin/restart.sh
```

如果想让代理更早暴露上游卡死（比如希望 60 秒静默就告诉用户重试，而不是等 10 分钟），启用：

```bash
export PROXY_UPSTREAM_IDLE_LIMIT=60
./bin/restart.sh
```

启用后代理会在 60 秒静默时向客户端写一个标准 SSE 错误事件（`event: error` + `event: message_stop`），Claude CLI 显示明确的报错并清晰结束流，便于用户重试。

### Q: 上游 429 / 网络抖动会自动重试吗？

A: 会。代理实现了 SDK 风格的重试策略（与 `@anthropic-ai/sdk` 行为一致）：

- **触发条件**：网络错误（`ConnectionError` / `TimeoutError` / `OSError`）、HTTP 408 / 409 / 429 / 500 / 502 / 503 / 504
- **重试次数**：默认 2 次（共 3 次尝试），`--max-retries` 调整
- **退避算法**：指数退避 + 全抖动（`uniform(0, min(8, 0.5 * 2^attempt))`），尊重 `Retry-After` 头
- **重要约束**：**只在流开始前重试**，一旦上游已经开始返回 SSE 字节，绝不自动重试（避免破坏已计费的对话状态）

---

## 安全建议

- `config.json` 包含真实 token，**不要提交到代码仓库**；请加入 `.gitignore`。
- 代理默认监听 `0.0.0.0`，如果你不需要在多机访问，建议绑定 `127.0.0.1`：  
  `./bin/start.sh` 修改 / 直接 `python3 proxy.py --host 127.0.0.1`
- `~/.claude/settings.json` 会被代理自动改写，原文件会备份为 `~/.claude/settings.json.bak`。

---

## 开发说明

- 单文件实现，仅依赖 Python 3 标准库
- 详见 `PLAN.md` 中的设计与权衡
