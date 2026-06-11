# Claude Code API Proxy 服务 — 实现计划

## Context

用户需要一个轻量级的 API 代理服务，介于 Claude Code CLI 和 Anthropic API 之间。核心需求：支持多模型配置路由（不同模型指向不同 API Key / Base URL）、热更新配置、以及完整的 API 代理能力（含流式响应）。

## 项目结构

```
/Users/zhangmang/WorkSpace/Python/claude_proxy/
├── proxy.py              # 主程序（单文件，基于 http.server）
├── config.json           # 配置文件（模型映射）
├── docs/
│   └── api-proxy.md      # 文档：需要代理的 API 接口说明
└── README.md             # 使用说明
```

## 架构设计

### 整体流程

```
Claude Code CLI  →  代理服务（:8080）  →  对应的 Anthropic API（根据模型路由）
                        ↑
                  读取 config.json 决定路由
```

### 核心类：`ProxyHTTPRequestHandler(http.server.BaseHTTPRequestHandler)`

### 需要代理的 API 端点

| 端点 | 方法 | 说明 | 路由逻辑 |
|---|---|---|---|
| `/v1/models` | GET | 返回可用模型列表 | 读取 config.json 的 key 列表 |
| `/v1/messages` | POST | 消息请求（含流式） | 根据 body 中 model 字段路由 |
| `/v1/messages/count_tokens` | POST | Token 计数 | 根据 body 中 model 字段路由 |
| `/v1/messages/batches` | POST | 批量请求 | 根据 body 中 model 字段路由 |
| `/v1/messages/batches/{id}` | GET | 批量状态查询 | 透传 |
| `/v1/messages/batches/{id}/results` | GET | 批量结果 | 透传 |
| `/v1/files` | POST | 上传文件 | 透传 |
| `/v1/files` | GET | 文件列表 | 透传 |
| `/v1/files/{file_id}` | GET | 文件元数据 | 透传 |
| `/v1/files/{file_id}` | DELETE | 删除文件 | 透传 |
| `/admin/reload` | POST/GET | 热加载配置 | 重读 config.json |

### 配置管理

```python
_config = {}  # 全局配置缓存

def load_config():
    global _config
    with open("config.json") as f:
        _config = json.load(f)
```

### 模型路由逻辑

对于 `POST /v1/messages` 和 `POST /v1/messages/count_tokens`：
1. 解析请求体 JSON，读取 `model` 字段（如 `"opus-4.6"`）
2. 在 `_config` 中查找对应条目
3. 如果找到 → 使用该条目的 `ANTHROPIC_AUTH_TOKEN` 和 `ANTHROPIC_BASE_URL` 转发请求
4. 如果未找到 → 返回 400 错误，提示模型不可用

### 流式响应处理（关键）

对于 `POST /v1/messages` 的 SSE 流式响应：
- 使用 `http.client` 或 `urllib.request` 转发请求
- 设置 `stream=True`（实际通过 urllib 读取响应体）
- 逐行读取响应体，逐行写回客户端
- 设置正确的 `Transfer-Encoding: chunked` 或逐行 flush
- 复制所有 Anthropic 响应头（`request-id`、`anthropic-organization-id` 等）

### /v1/models 响应的关键能力字段

为了让 Claude Code 识别这些模型并启用全部功能（thinking、image input、PDF input 等），返回的模型对象需要包含完整的 `capabilities` 字段。

### 启动方式

```bash
python3 proxy.py [--port 8080] [--config config.json]
```

## 实施步骤

1. **创建目录结构**：`docs/` 目录
2. **编写文档** `docs/api-proxy.md`：详细说明所有需要代理的 API 端点、请求/响应格式、Headers
3. **编写配置文件示例** `config.json`
4. **编写主程序** `proxy.py`（单文件实现）
5. **编写 README.md**：使用说明

## 验证方式

1. 启动代理服务：`python3 proxy.py`
2. 测试热更新：`curl http://localhost:8080/admin/reload`
3. 测试模型列表：`curl http://localhost:8080/v1/models`
4. 配置 Claude Code 使用代理：设置 `ANTHROPIC_BASE_URL=http://localhost:8080`
5. 在 Claude Code 中使用 `/model` 切换模型，验证路由正确