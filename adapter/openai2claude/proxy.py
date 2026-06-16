#!/usr/bin/env python3
"""
OpenAI → Claude Adapter Proxy

A proxy that lets the Claude Code CLI talk to OpenAI-compatible Chat
Completions endpoints. From the CLI's point of view this proxy speaks the
Anthropic Messages API; internally it translates each request into an
OpenAI Chat Completions request, and translates the (streaming or
non-streaming) response back into Anthropic Messages events.

Endpoints:
    GET  /v1/models                  - List configured models
    POST /v1/messages                - Anthropic Messages API (stream supported)
    POST /v1/messages/count_tokens   - Token-count estimate
    POST/GET /admin/reload           - Hot-reload config.json (and sync ~/.claude/settings.json)

Usage:
    python3 proxy.py [--port 8080] [--config config.json]

Layout / logging conventions mirror ../claude_proxy/proxy.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import queue
import random
import re
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

# ── Logging ──────────────────────────────────────────────────────────────────

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

_DEFAULT_LOG_DIR = Path(__file__).resolve().parent / "log"
_DEFAULT_LOG_FILENAME = "proxy.log"
_DEFAULT_LOG_MAX_BYTES = 100 * 1024 * 1024  # 100 MB
_DEFAULT_LOG_BACKUP_COUNT = 6

# Logging policy (same as claude_proxy/proxy.py):
#   - logger.* output  -> ONLY proxy.log (RotatingFileHandler)
#   - print(...)       -> stdout -> nohup -> proxy.out
#   - print(file=err)  -> stderr -> nohup -> proxy.err
logger = logging.getLogger("openai2claude")
logger.setLevel(logging.INFO)
_root_logger = logging.getLogger()
_root_logger.setLevel(logging.INFO)
if not any(isinstance(h, logging.NullHandler) for h in _root_logger.handlers):
    _root_logger.addHandler(logging.NullHandler())


def setup_file_logging(
    log_dir=None,
    filename=_DEFAULT_LOG_FILENAME,
    max_bytes=_DEFAULT_LOG_MAX_BYTES,
    backup_count=_DEFAULT_LOG_BACKUP_COUNT,
):
    """Attach a RotatingFileHandler so logger.* goes to a rolling file."""
    log_dir = Path(log_dir) if log_dir else _DEFAULT_LOG_DIR
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error("Failed to create log directory %s: %s", log_dir, e)
        return None

    log_path = log_dir / filename
    handler = logging.handlers.RotatingFileHandler(
        filename=str(log_path),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
    handler.setLevel(logging.INFO)

    root_logger = logging.getLogger()
    for h in list(root_logger.handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler) and \
                getattr(h, "baseFilename", None) == handler.baseFilename:
            root_logger.removeHandler(h)
    for h in list(root_logger.handlers):
        if type(h) is logging.StreamHandler:
            root_logger.removeHandler(h)
    root_logger.addHandler(handler)

    logger.info(
        "File logging enabled: path=%s max_bytes=%d backup_count=%d",
        log_path, max_bytes, backup_count,
    )
    return log_path


# ── Configuration ────────────────────────────────────────────────────────────

_config = {}
_config_path = ""
_loaded = False
_server_host = "0.0.0.0"
_server_port = 8080

_CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


def load_config(config_path=None):
    """Load config.json. Each entry maps a Claude-style model id to OpenAI upstream info."""
    global _config, _config_path, _loaded

    if config_path:
        _config_path = config_path

    try:
        with open(_config_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # Drop "_comment" pseudo-fields; keep ordering of the JSON file.
        cleaned = {}
        for k, v in raw.items():
            if k.startswith("_"):
                continue  # documentation-only top-level keys
            if isinstance(v, dict):
                cleaned[k] = {kk: vv for kk, vv in v.items() if kk != "_comment"}
        _config = cleaned
        _loaded = True

        keys = list(_config.keys())
        logger.info("Configuration loaded: %d model(s) - %s", len(keys), ", ".join(keys))
        if len(keys) not in (3, 4):
            logger.warning(
                "config.json should contain 3 or 4 models (got %d). "
                "Slot mapping will fall back where missing.", len(keys),
            )
        return True
    except FileNotFoundError:
        logger.error("Configuration file not found: %s", _config_path)
        _loaded = False
        return False
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in configuration file: %s", e)
        _loaded = False
        return False


def get_config():
    return _config


def get_model_config(model_name):
    return _config.get(model_name)


def is_loaded():
    return _loaded


# ── Claude Code settings sync ────────────────────────────────────────────────

_CLAUDE_MODEL_SLOTS = ("opus", "sonnet", "haiku", "fable")


def _pick_model_for_slot(slot, model_ids):
    """Strict positional fallback: 1st->opus, 2nd->sonnet, 3rd->haiku, 4th->fable."""
    if not model_ids:
        return None
    idx = {"opus": 0, "sonnet": 1, "haiku": 2, "fable": 3}.get(slot.lower())
    if idx is None:
        return model_ids[0]
    if idx < len(model_ids):
        return model_ids[idx]
    return model_ids[-1]


def _build_slot_mapping(config):
    """Build Claude family slot mapping from config.

    Each model entry may optionally set:
        "mapping_model": "opus" | "sonnet" | "haiku" | "fable"

    Explicit mapping_model wins for that slot. Slots without explicit mapping
    keep the historical positional fallback behavior for backward compatibility.
    Invalid/duplicate mapping_model values are ignored with warnings rather than
    breaking startup.
    """
    model_ids = list(config.keys())
    mapping = {slot: _pick_model_for_slot(slot, model_ids) for slot in _CLAUDE_MODEL_SLOTS}
    explicit = {}

    for model_id in model_ids:
        entry = config.get(model_id) or {}
        raw_slot = entry.get("mapping_model")
        if raw_slot in (None, ""):
            continue
        if not isinstance(raw_slot, str):
            logger.warning(
                "Ignoring non-string mapping_model for model=%s value=%r",
                model_id, raw_slot,
            )
            continue
        slot = raw_slot.strip().lower()
        if slot not in _CLAUDE_MODEL_SLOTS:
            logger.warning(
                "Ignoring invalid mapping_model for model=%s value=%r; expected one of %s",
                model_id, raw_slot, ",".join(_CLAUDE_MODEL_SLOTS),
            )
            continue
        if slot in explicit:
            logger.warning(
                "Duplicate mapping_model=%s: keeping first model=%s, ignoring model=%s",
                slot, explicit[slot], model_id,
            )
            continue
        mapping[slot] = model_id
        explicit[slot] = model_id

    return mapping, explicit


def _proxy_base_url():
    host = _server_host
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return f"http://{host}:{_server_port}"


def sync_claude_settings():
    """Point ~/.claude/settings.json at this proxy and slot the configured models."""
    model_ids = list(_config.keys())
    if not model_ids:
        return False, {"error": "No models in proxy configuration; nothing to sync"}

    mapping, explicit_mapping = _build_slot_mapping(_config)
    opus_id = mapping["opus"]
    sonnet_id = mapping["sonnet"]
    haiku_id = mapping["haiku"]
    fable_id = mapping["fable"]

    base_url = _proxy_base_url()
    default_model = model_ids[0]

    env_block = {
        "ANTHROPIC_AUTH_TOKEN": "openai2claude-local-token",
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_MODEL": default_model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": opus_id,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": sonnet_id,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": haiku_id,
        "ANTHROPIC_DEFAULT_FABLE_MODEL": fable_id,
        "ANTHROPIC_SMALL_FAST_MODEL": haiku_id,
        "CLAUDE_CODE_EFFORT_LEVEL": "max",
    }

    settings = {}
    if _CLAUDE_SETTINGS_PATH.exists():
        try:
            with open(_CLAUDE_SETTINGS_PATH, "r", encoding="utf-8") as f:
                settings = json.load(f) or {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Existing %s is unreadable (%s); will overwrite.", _CLAUDE_SETTINGS_PATH, e)
            settings = {}

    existing_env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    existing_env.update(env_block)
    settings["env"] = existing_env
    settings["model"] = "opus"

    try:
        _CLAUDE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        if _CLAUDE_SETTINGS_PATH.exists():
            backup = _CLAUDE_SETTINGS_PATH.with_suffix(".json.bak")
            try:
                backup.write_text(
                    _CLAUDE_SETTINGS_PATH.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            except OSError as e:
                logger.warning("Could not write backup %s: %s", backup, e)
        with open(_CLAUDE_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except OSError as e:
        logger.error("Failed to write %s: %s", _CLAUDE_SETTINGS_PATH, e)
        return False, {"error": f"Failed to write settings: {e}"}

    mapping_source = {
        slot: "explicit" if slot in explicit_mapping else "positional_fallback"
        for slot in _CLAUDE_MODEL_SLOTS
    }
    logger.info(
        "Synced %s -> base_url=%s, default=%s, mapping=%s, mapping_source=%s",
        _CLAUDE_SETTINGS_PATH, base_url, default_model, mapping, mapping_source,
    )
    return True, {
        "path": str(_CLAUDE_SETTINGS_PATH),
        "base_url": base_url,
        "default_model": default_model,
        "mapping": mapping,
        "mapping_source": mapping_source,
    }


# ── /v1/models response ──────────────────────────────────────────────────────

_DISPLAY_NAMES = {}  # populated lazily from config keys when /v1/models is hit


def get_model_display_name(model_id):
    return _DISPLAY_NAMES.get(model_id, model_id)


def build_models_response():
    data = []
    for model_id, entry in _config.items():
        upstream = entry.get("upstream_model", "?")
        data.append({
            "type": "model",
            "id": model_id,
            "display_name": f"{model_id} (→ {upstream})",
            "created_at": "2026-01-01T00:00:00Z",
        })
    model_ids = [m["id"] for m in data]
    return {
        "data": data,
        "first_id": model_ids[0] if model_ids else None,
        "has_more": False,
        "last_id": model_ids[-1] if model_ids else None,
    }


# ── HTTP forwarding tunables (mirror claude_proxy) ───────────────────────────

_DEFAULT_HTTP_TIMEOUT = 600
_DEFAULT_STREAM_TIMEOUT = 600
_DEFAULT_STREAM_KEEPALIVE = 10
# 90s = upstream said 200 OK but produced 0 bytes for this long.
# Common pathology with deepseek/glm gateways under load — connection
# stays open silently. Without this guard, downstream waits the full
# stream-idle (10min) before giving up. Set to 0 to disable.
_DEFAULT_UPSTREAM_IDLE_LIMIT = 90
_DEFAULT_MAX_RETRIES = 2
_DEFAULT_RETRY_BASE_DELAY = 0.5
# Mirror Anthropic's behaviour: when the upstream returns an effectively empty
# `stop` turn (no text, no reasoning, no tool, completion_tokens<=1) we retry
# the same payload a few times *inside* the proxy instead of forwarding the
# empty turn to Claude Code (which would treat it as `end_turn` and enter a
# multi-minute idle/backoff). Set to 0 to disable.
_DEFAULT_EMPTY_STOP_MAX_RETRY = 2
_DEFAULT_EMPTY_STOP_BASE_DELAY = 0.5
# 429 (rate-limit) is treated specially: upstream gateways often respond
# with 429 in bursts when the per-key QPS bucket is empty. A short random
# backoff like the generic one (potentially as small as 10ms) just amplifies
# the burst. We use a longer floor + exponential growth + small jitter, and
# allow more attempts than other retriable statuses so the proxy absorbs the
# rate-limit instead of leaking it to Claude Code (which would itself retry,
# making the storm worse).
_DEFAULT_RATE_LIMIT_MAX_RETRIES = 5
_DEFAULT_RATE_LIMIT_BASE_DELAY = 1.0
_DEFAULT_RATE_LIMIT_CAP = 30.0

_http_timeout = _DEFAULT_HTTP_TIMEOUT
_stream_timeout = _DEFAULT_STREAM_TIMEOUT
_stream_keepalive = _DEFAULT_STREAM_KEEPALIVE
_upstream_idle_limit = _DEFAULT_UPSTREAM_IDLE_LIMIT
_max_retries = _DEFAULT_MAX_RETRIES
_empty_stop_max_retry = _DEFAULT_EMPTY_STOP_MAX_RETRY

_RETRIABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


def configure_timeouts(
    http_timeout=None, stream_timeout=None, stream_keepalive=None,
    max_retries=None, upstream_idle_limit=None, empty_stop_max_retry=None,
):
    global _http_timeout, _stream_timeout, _stream_keepalive, _max_retries
    global _upstream_idle_limit, _empty_stop_max_retry
    if http_timeout is not None:
        _http_timeout = int(http_timeout)
    if stream_timeout is not None:
        _stream_timeout = int(stream_timeout)
    if stream_keepalive is not None:
        _stream_keepalive = int(stream_keepalive)
    if max_retries is not None:
        _max_retries = max(0, int(max_retries))
    if upstream_idle_limit is not None:
        _upstream_idle_limit = max(0, int(upstream_idle_limit))
    if empty_stop_max_retry is not None:
        _empty_stop_max_retry = max(0, int(empty_stop_max_retry))


def _compute_backoff(attempt, retry_after_header=None):
    if retry_after_header:
        try:
            return max(0.0, float(retry_after_header))
        except (TypeError, ValueError):
            pass
    base = _DEFAULT_RETRY_BASE_DELAY * (2 ** attempt)
    cap = 8.0
    return random.uniform(0.0, min(cap, base))


def _compute_rate_limit_backoff(attempt, retry_after_header=None):
    """Backoff specifically for 429 responses.

    - Honors `Retry-After` when the gateway provides it.
    - Otherwise: floor at _DEFAULT_RATE_LIMIT_BASE_DELAY, exponential ramp,
      capped, with a small additive jitter so concurrent workers de-correlate.
    """
    if retry_after_header:
        try:
            return max(0.0, float(retry_after_header))
        except (TypeError, ValueError):
            pass
    base = _DEFAULT_RATE_LIMIT_BASE_DELAY * (2 ** attempt)
    delay = min(_DEFAULT_RATE_LIMIT_CAP, base)
    # Add jitter in [0, base/2) but never below the floor.
    return delay + random.uniform(0.0, max(0.0, delay * 0.25))


def _tune_long_idle_socket(sock):
    if sock is None:
        return
    idle_optname = None
    if hasattr(socket, "TCP_KEEPIDLE"):
        idle_optname = socket.TCP_KEEPIDLE
    elif hasattr(socket, "TCP_KEEPALIVE"):
        idle_optname = socket.TCP_KEEPALIVE
    elif sys.platform == "darwin":
        idle_optname = 0x10
    elif sys.platform.startswith("linux"):
        idle_optname = 4
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if idle_optname is not None:
            sock.setsockopt(socket.IPPROTO_TCP, idle_optname, 30)
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 15)
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)
    except (OSError, AttributeError) as ke:
        logger.warning("setsockopt(keepalive) failed: %s", ke)


def forward_openai_request(target_base, target_path, headers, body, stream=False):
    """Forward a JSON HTTP POST/GET to an OpenAI-compatible endpoint with retries."""
    import http.client

    if "://" not in target_base:
        target_base = "https://" + target_base
    parsed = urlparse(target_base)
    scheme = parsed.scheme or "https"
    host = parsed.hostname or ""
    port = parsed.port or (443 if scheme == "https" else 80)
    base_path = parsed.path.rstrip("/")

    if target_path.startswith("/"):
        full_path = base_path + target_path
    else:
        full_path = base_path + "/" + target_path
    if not full_path.startswith("/"):
        full_path = "/" + full_path

    fwd_headers = dict(headers)
    fwd_headers["Accept-Encoding"] = "identity"
    if body:
        fwd_headers["Content-Length"] = str(len(body))

    use_ssl = scheme == "https"
    sock_timeout = _stream_timeout if stream else _http_timeout

    last_error = None
    # Loop bound is the larger of the generic retry budget and the rate-limit
    # budget; per-attempt logic below decides whether to actually retry based
    # on the specific status code.
    loop_max = max(_max_retries, _DEFAULT_RATE_LIMIT_MAX_RETRIES)
    for attempt in range(loop_max + 1):
        conn = None
        try:
            if use_ssl:
                conn = http.client.HTTPSConnection(host, port, timeout=sock_timeout)
            else:
                conn = http.client.HTTPConnection(host, port, timeout=sock_timeout)
            conn.request("POST", full_path, body=body, headers=fwd_headers)
            try:
                if hasattr(conn, "sock") and conn.sock is not None:
                    _tune_long_idle_socket(conn.sock)
                    conn.sock.settimeout(sock_timeout)
            except Exception:
                pass
            response = conn.getresponse()
            if stream and response.fp is not None:
                try:
                    sock = response.fp.raw._sock if hasattr(response.fp, "raw") else None
                    if sock is None and hasattr(conn, "sock"):
                        sock = conn.sock
                    if sock is not None:
                        sock.settimeout(_stream_timeout)
                        _tune_long_idle_socket(sock)
                except Exception:
                    pass
            resp_headers = dict(response.getheaders())

            # 429 has its own retry budget and backoff curve so a rate-limit
            # storm is absorbed inside the proxy instead of being forwarded to
            # Claude Code (which would retry on top, amplifying the storm).
            is_rate_limited = response.status == 429
            effective_max = (
                max(_max_retries, _DEFAULT_RATE_LIMIT_MAX_RETRIES)
                if is_rate_limited else _max_retries
            )
            if response.status in _RETRIABLE_STATUS and attempt < effective_max:
                retry_after = resp_headers.get("Retry-After") or resp_headers.get("retry-after")
                try:
                    response.read(2048)
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
                if is_rate_limited:
                    delay = _compute_rate_limit_backoff(attempt, retry_after)
                else:
                    delay = _compute_backoff(attempt, retry_after)
                logger.warning(
                    "Upstream returned %d (attempt %d/%d%s). Retrying in %.2fs (Retry-After=%s)",
                    response.status, attempt + 1, effective_max + 1,
                    " rate-limit" if is_rate_limited else "",
                    delay, retry_after,
                )
                time.sleep(delay)
                continue

            if stream:
                return response.status, resp_headers, response, conn
            else:
                resp_body = response.read()
                conn.close()
                return response.status, resp_headers, resp_body

        except (TimeoutError, ConnectionError, OSError) as e:
            last_error = e
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            if attempt < _max_retries:
                delay = _compute_backoff(attempt)
                logger.warning(
                    "Network error contacting %s://%s:%s%s (attempt %d/%d): %s. Retrying in %.2fs",
                    scheme, host, port, full_path, attempt + 1, _max_retries + 1, e, delay,
                )
                time.sleep(delay)
                continue
            logger.error("Error forwarding to %s://%s:%s%s: %s", scheme, host, port, full_path, e)
            raise

    if last_error is not None:
        raise last_error
    raise RuntimeError("forward_openai_request: exhausted retries with no exception captured")


# ── Standard protocol objects and converter interface ────────────────────────

@dataclass
class ClaudeMessagesRequest:
    """Normalized Anthropic Messages request used inside this adapter."""

    model: str
    messages: list = field(default_factory=list)
    system: object = None
    tools: list = field(default_factory=list)
    tool_choice: object = None
    max_tokens: int | None = None
    temperature: object = None
    top_p: object = None
    stop_sequences: object = None
    stream: bool = False
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw):
        raw = raw if isinstance(raw, dict) else {}
        return cls(
            model=raw.get("model") or "",
            messages=raw.get("messages") or [],
            system=raw.get("system"),
            tools=raw.get("tools") or [],
            tool_choice=raw.get("tool_choice"),
            max_tokens=raw.get("max_tokens"),
            temperature=raw.get("temperature"),
            top_p=raw.get("top_p"),
            stop_sequences=raw.get("stop_sequences"),
            stream=bool(raw.get("stream", False)),
            raw=raw,
        )


@dataclass
class OpenAIChatRequest:
    """Normalized OpenAI Chat Completions request used inside this adapter."""

    model: str
    messages: list = field(default_factory=list)
    stream: bool = False
    max_tokens_field: str = "max_tokens"
    max_tokens: object = None
    temperature: object = None
    top_p: object = None
    stop: object = None
    tools: object = None
    tool_choice: object = None
    stream_options: object = None
    raw_extra: dict = field(default_factory=dict)

    def to_openai_dict(self):
        data = {
            "model": self.model,
            "messages": self.messages,
            "stream": self.stream,
        }
        if self.max_tokens is not None:
            data[self.max_tokens_field] = self.max_tokens
        if self.temperature is not None:
            data["temperature"] = self.temperature
        if self.top_p is not None:
            data["top_p"] = self.top_p
        if self.stop is not None:
            data["stop"] = self.stop
        if self.tools:
            data["tools"] = self.tools
        if self.tool_choice is not None:
            data["tool_choice"] = self.tool_choice
        if self.stream_options is not None:
            data["stream_options"] = self.stream_options
        data.update(self.raw_extra)
        return data


@dataclass
class OpenAIChatCompletion:
    """Normalized complete OpenAI Chat Completions response."""

    id: str
    model: str | None = None
    message: dict = field(default_factory=dict)
    finish_reason: str | None = None
    usage: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw):
        raw = raw if isinstance(raw, dict) else {}
        choice = (raw.get("choices") or [{}])[0]
        return cls(
            id=raw.get("id") or f"chatcmpl_{uuid.uuid4().hex}",
            model=raw.get("model"),
            message=choice.get("message") or {},
            finish_reason=choice.get("finish_reason") or "stop",
            usage=raw.get("usage") or {},
            raw=raw,
        )


@dataclass
class ClaudeMessagesResponse:
    """Normalized Anthropic Messages response."""

    id: str
    model: str
    content: list = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: dict = field(default_factory=dict)

    def to_anthropic_dict(self):
        return {
            "id": self.id,
            "type": "message",
            "role": "assistant",
            "model": self.model,
            "content": self.content,
            "stop_reason": self.stop_reason,
            "stop_sequence": None,
            "usage": self.usage,
        }


class ClaudeOpenAIConverter:
    """Single conversion boundary between normalized Claude and OpenAI objects."""

    def claude_request_to_openai(self, claude_req, upstream_model, cfg=None):
        cfg = cfg or {}
        flags = _resolve_compat_flags(cfg)
        tools = _anthropic_tools_to_openai(claude_req.tools)
        tool_choice = _anthropic_tool_choice_to_openai(claude_req.tool_choice)
        # When the upstream is in "thinking" mode (deepseek-v4-pro, glm-thinking,
        # kimi-k2-thinking, etc.), the gateway requires that any assistant
        # `reasoning_content` we previously surfaced as Anthropic `thinking`
        # blocks be echoed back on the corresponding assistant message in the
        # next request. Otherwise it returns:
        #   400 "The `reasoning_content` in the thinking mode must be passed
        #        back to the API."
        preserve_reasoning = flags.get("reasoning_mode") == "thinking"
        return OpenAIChatRequest(
            model=upstream_model,
            messages=_anthropic_messages_to_openai(
                claude_req.messages, claude_req.system,
                preserve_reasoning=preserve_reasoning,
            ),
            stream=claude_req.stream,
            max_tokens_field=flags["max_tokens_field"],
            max_tokens=claude_req.max_tokens,
            temperature=claude_req.temperature if flags["send_temperature"] else None,
            top_p=claude_req.top_p if flags["send_top_p"] else None,
            stop=claude_req.stop_sequences if flags["send_stop"] else None,
            tools=tools,
            tool_choice=tool_choice,
        )

    def openai_completion_to_claude(self, openai_completion, anthropic_model):
        content_blocks = self.openai_message_to_claude_blocks(openai_completion.message)
        stop_reason = self.openai_stop_reason_to_claude(
            openai_completion.message, openai_completion.finish_reason,
        )
        usage = openai_completion.usage or {}
        return ClaudeMessagesResponse(
            id=openai_completion.id or f"msg_{uuid.uuid4().hex}",
            model=anthropic_model,
            content=content_blocks,
            stop_reason=stop_reason,
            usage={
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        )

    def openai_message_to_claude_blocks(self, message):
        return _openai_message_to_anthropic_blocks(message)

    def openai_stop_reason_to_claude(self, openai_message, finish_reason):
        return _anthropic_stop_reason_for_message(openai_message, finish_reason)


_CONVERTER = ClaudeOpenAIConverter()


# ── Anthropic ↔ OpenAI translation helpers ───────────────────────────────────

def _flatten_anthropic_text(content):
    """Anthropic content can be a str or a list of blocks. Return plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if not isinstance(blk, dict):
                continue
            t = blk.get("type")
            if t == "text":
                parts.append(blk.get("text", ""))
            elif t == "tool_result":
                # Inline tool results into the text channel; OpenAI will see it
                # as content of a "tool" role message we craft below.
                inner = blk.get("content", "")
                if isinstance(inner, list):
                    for sub in inner:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            parts.append(sub.get("text", ""))
                elif isinstance(inner, str):
                    parts.append(inner)
        return "\n".join(parts)
    return ""


def _anthropic_messages_to_openai(messages, system, preserve_reasoning=False):
    """Convert Anthropic messages[] + system field into OpenAI messages[].

    When `preserve_reasoning` is True (upstream in thinking mode), Anthropic
    `thinking` content blocks on assistant turns are forwarded to the upstream
    as the OpenAI-extension `reasoning_content` field on the assistant message.
    Some gateways (deepseek-v4-pro, glm-thinking, kimi-k2-thinking) reject
    requests that drop these on continuation turns:
        400 "The `reasoning_content` in the thinking mode must be passed
             back to the API."
    """
    out = []
    if system:
        if isinstance(system, list):
            sys_text = "\n".join(
                blk.get("text", "")
                for blk in system if isinstance(blk, dict) and blk.get("type") == "text"
            )
        else:
            sys_text = str(system)
        if sys_text:
            out.append({"role": "system", "content": sys_text})

    dropped_tool_use_ids = set()

    for msg_index, msg in enumerate(messages or []):
        role = msg.get("role")
        content = msg.get("content")

        if role == "assistant":
            # Assistant turn may contain text + tool_use + thinking blocks
            text_parts = []
            reasoning_parts = []
            tool_calls = []
            if isinstance(content, list):
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("type") == "text":
                        text_parts.append(blk.get("text", ""))
                    elif blk.get("type") == "thinking":
                        # Anthropic native thinking block. Preserve into the
                        # OpenAI-extension `reasoning_content` field when the
                        # upstream is in thinking mode.
                        if preserve_reasoning:
                            t = blk.get("thinking")
                            if isinstance(t, str) and t:
                                reasoning_parts.append(t)
                    elif blk.get("type") == "redacted_thinking":
                        # Encrypted/redacted reasoning blocks have no plaintext
                        # we can echo back; skip silently.
                        continue
                    elif blk.get("type") == "tool_use":
                        name = str(blk.get("name") or "").strip()
                        tool_id = blk.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                        if not name:
                            # Defensive cleanup for historical/corrupt Claude context:
                            # older buggy emitter could generate tool_use blocks with
                            # empty name. OpenAI-compatible APIs reject these as
                            # `messages[i].tool_calls[j].function.name: empty string`.
                            # Drop the bad call and remember its id so its following
                            # tool_result can be dropped too.
                            dropped_tool_use_ids.add(tool_id)
                            logger.warning(
                                "Dropping assistant tool_use with empty name "
                                "at messages[%d] id=%s",
                                msg_index, tool_id,
                            )
                            continue
                        tool_calls.append({
                            "id": tool_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(
                                    blk.get("input", {}), ensure_ascii=False
                                ),
                            },
                        })
            else:
                text_parts.append(_flatten_anthropic_text(content))
            asst = {"role": "assistant", "content": "\n".join(text_parts) or None}
            if tool_calls:
                asst["tool_calls"] = tool_calls
                if asst["content"] is None:
                    asst["content"] = ""
            if reasoning_parts:
                # OpenAI-extension field used by deepseek / glm-thinking /
                # kimi-k2-thinking / qwen-thinking gateways. Harmless on
                # standard OpenAI (gpt-4o etc.) since unknown fields are
                # ignored, but skip to be safe when the caller didn't ask.
                asst["reasoning_content"] = "".join(reasoning_parts)
            if asst["content"] is None and not tool_calls and not reasoning_parts:
                # Entire assistant turn may have contained only corrupt/empty-name
                # tool_use blocks that were dropped above. Do not forward an empty
                # assistant message to OpenAI.
                logger.warning("Dropping empty assistant message at messages[%d]", msg_index)
                continue
            # Coalesce with the immediately-preceding assistant message.
            #
            # Claude Code persists a single upstream assistant turn as multiple
            # separate session records when the block types differ
            # (e.g. one record per [thinking], [text], [tool_use]). On the next
            # request it re-sends those as N adjacent assistant messages with
            # role="assistant". If we forward them verbatim to OpenAI, the
            # `reasoning_content` ends up on a different message than the
            # `tool_calls`, and reasoning-mode gateways
            # (deepseek-v4-pro / glm-thinking / kimi-k2-thinking) reject it:
            #   400 "The `reasoning_content` in the thinking mode must be
            #        passed back to the API."
            # Merging back into one OpenAI assistant message restores the
            # original turn semantics and satisfies the gateway contract.
            if out and out[-1].get("role") == "assistant":
                prev = out[-1]
                prev_text = prev.get("content") or ""
                new_text = asst.get("content") or ""
                if prev_text and new_text:
                    prev["content"] = prev_text + "\n" + new_text
                elif new_text:
                    prev["content"] = new_text
                # else: keep prev["content"] as-is
                if tool_calls:
                    prev_tc = prev.get("tool_calls") or []
                    prev["tool_calls"] = prev_tc + tool_calls
                    if prev.get("content") is None:
                        prev["content"] = ""
                if reasoning_parts:
                    prev_reason = prev.get("reasoning_content") or ""
                    prev["reasoning_content"] = prev_reason + "".join(reasoning_parts)
                continue
            out.append(asst)
            continue

        if role == "user":
            # A user message may carry tool_result blocks that should map to
            # OpenAI 'tool' role messages keyed by tool_use_id.
            if isinstance(content, list):
                tool_msgs = []
                text_parts = []
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("type") == "tool_result":
                        tool_use_id = blk.get("tool_use_id", "")
                        if tool_use_id in dropped_tool_use_ids:
                            logger.warning(
                                "Dropping tool_result for previously dropped empty-name tool_use "
                                "at messages[%d] tool_use_id=%s",
                                msg_index, tool_use_id,
                            )
                            continue
                        tool_msgs.append({
                            "role": "tool",
                            "tool_call_id": tool_use_id,
                            "content": _flatten_anthropic_text(blk.get("content", "")),
                        })
                    elif blk.get("type") == "text":
                        text_parts.append(blk.get("text", ""))
                if tool_msgs:
                    out.extend(tool_msgs)
                if text_parts:
                    out.append({"role": "user", "content": "\n".join(text_parts)})
            else:
                out.append({"role": "user", "content": _flatten_anthropic_text(content)})
            continue

        # Fallback for any other role
        out.append({"role": role or "user", "content": _flatten_anthropic_text(content)})

    return out


def _anthropic_tools_to_openai(tools):
    if not tools:
        return None
    openai_tools = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        openai_tools.append({
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    return openai_tools


def _anthropic_tool_choice_to_openai(tool_choice):
    if not tool_choice:
        return None
    if isinstance(tool_choice, dict):
        t = tool_choice.get("type")
        if t == "auto":
            return "auto"
        if t == "any":
            return "required"
        if t == "tool" and tool_choice.get("name"):
            return {"type": "function", "function": {"name": tool_choice["name"]}}
    return None


# ── Provider / model compatibility profiles ──────────────────────────────────
#
# `provider` means the service/vendor only (openai, deepseek, glm, ...).
# Model-family quirks are expressed separately via `model_series` or
# `model_code` (e.g. "gpt-5.5", "o3", "reasoning"). This avoids overloading
# provider with protocol/model behavior.
#
# A profile encodes quirks of one /v1/chat/completions target:
#     max_tokens_field : "max_tokens" | "max_completion_tokens"
#     send_temperature : bool
#     send_top_p       : bool
#     send_stop        : bool

_STANDARD_CHAT_PROFILE = {
    "max_tokens_field": "max_tokens",
    "send_temperature": True,
    "send_top_p": True,
    "send_stop": True,
    # `reasoning_mode` controls how upstream `delta.reasoning_content`
    # (deepseek-v4-pro / o-series / glm-zero / kimi-k2-thinking / etc.) is
    # surfaced into the Anthropic stream we hand back to Claude Code:
    #   "thinking" : emit native Anthropic `thinking` content blocks. Best
    #                for Claude Code (renders in UI, prevents "No assistant
    #                messages found" when the model spends *all* tokens in
    #                the reasoning channel).
    #   "text"     : merge reasoning into the normal text stream wrapped in
    #                a `<thinking>...</thinking>` envelope. Use for
    #                non-Claude consumers that don't understand the
    #                thinking block type.
    #   "drop"     : silently discard. Same as historical behavior; only
    #                safe when the model never streams content via
    #                reasoning_content.
    "reasoning_mode": "drop",
}

_REASONING_PROFILE = {
    "max_tokens_field": "max_completion_tokens",
    "send_temperature": False,
    "send_top_p": False,
    "send_stop": False,
    "reasoning_mode": "thinking",
}

# Standard chat protocol but with an exposed reasoning channel.
# These accept temperature/top_p/stop and use `max_tokens`, but stream
# `delta.reasoning_content` separately from `delta.content`
# (deepseek-reasoner / deepseek-v3.x / deepseek-v4-pro / glm thinking
# variants / kimi-k2-thinking / qwen-thinking / ...).
_THINKING_CHAT_PROFILE = {
    "max_tokens_field": "max_tokens",
    "send_temperature": True,
    "send_top_p": True,
    "send_stop": True,
    "reasoning_mode": "thinking",
}

_PROVIDER_PROFILES = {
    # Service/vendor profiles. Keep these vendor-oriented only.
    "openai": _STANDARD_CHAT_PROFILE,
    "deepseek": _STANDARD_CHAT_PROFILE,
    "kimi": _STANDARD_CHAT_PROFILE,
    "moonshot": _STANDARD_CHAT_PROFILE,
    "qwen": _STANDARD_CHAT_PROFILE,
    "dashscope": _STANDARD_CHAT_PROFILE,
    "zhipu": _STANDARD_CHAT_PROFILE,
    "glm": _STANDARD_CHAT_PROFILE,
    "minimax": _STANDARD_CHAT_PROFILE,
    "together": _STANDARD_CHAT_PROFILE,
    "groq": _STANDARD_CHAT_PROFILE,
    "ollama": _STANDARD_CHAT_PROFILE,
    "vllm": _STANDARD_CHAT_PROFILE,
    "generic": _STANDARD_CHAT_PROFILE,
}

# Model-family / protocol quirks. `model_series` and `model_code` are matched
# against this map after provider is resolved, and may override provider flags.
_MODEL_SERIES_PROFILES = {
    # OpenAI-style reasoning models (closed thinking, max_completion_tokens,
    # no temperature/top_p/stop).
    "reasoning": _REASONING_PROFILE,
    "openai-reasoning": _REASONING_PROFILE,
    "gpt-5": _REASONING_PROFILE,
    "gpt-5.5": _REASONING_PROFILE,
    "gpt5": _REASONING_PROFILE,
    "gpt5.5": _REASONING_PROFILE,
    "o1": _REASONING_PROFILE,
    "o3": _REASONING_PROFILE,
    "o4": _REASONING_PROFILE,
    # Standard chat protocol but with an exposed reasoning channel.
    # These accept temperature/top_p/stop and use `max_tokens`.
    "deepseek-reasoner": _THINKING_CHAT_PROFILE,
    "deepseek-v3": _THINKING_CHAT_PROFILE,
    "deepseek-v4": _THINKING_CHAT_PROFILE,
    "deepseek-v4-pro": _THINKING_CHAT_PROFILE,
    "deepseek-r1": _THINKING_CHAT_PROFILE,
    "glm-4.6": _THINKING_CHAT_PROFILE,
    "glm-4.6-thinking": _THINKING_CHAT_PROFILE,
    "glm-zero": _THINKING_CHAT_PROFILE,
    "kimi-k2": _THINKING_CHAT_PROFILE,
    "kimi-k2-thinking": _THINKING_CHAT_PROFILE,
    "thinking": _THINKING_CHAT_PROFILE,
}

_DEFAULT_PROVIDER = "openai"


def _resolve_compat_flags(cfg):
    """Resolve per-request compat flags from provider + model_series/model_code.

    `provider` is the service/vendor (openai/deepseek/glm/etc.).
    `model_series` or `model_code` expresses model-family behavior (gpt-5.5,
    o3, reasoning, ...). Model profile wins over provider defaults.
    """
    provider = (cfg.get("provider") or _DEFAULT_PROVIDER).strip().lower()
    profile = dict(_PROVIDER_PROFILES.get(provider, _STANDARD_CHAT_PROFILE))

    raw_series = cfg.get("model_series") or cfg.get("model_code") or ""
    model_series = str(raw_series).strip().lower()
    model_profile = _MODEL_SERIES_PROFILES.get(model_series)
    if model_profile:
        profile.update(model_profile)

    profile["provider"] = provider
    profile["known_provider"] = provider in _PROVIDER_PROFILES
    profile["model_series"] = model_series
    profile["known_model_series"] = bool(model_profile)
    return profile


def anthropic_request_to_openai(req, upstream_model, cfg=None):
    """Translate an Anthropic /v1/messages request to OpenAI /v1/chat/completions."""
    claude_req = ClaudeMessagesRequest.from_raw(req)
    return _CONVERTER.claude_request_to_openai(
        claude_req, upstream_model, cfg,
    ).to_openai_dict()


# Map OpenAI finish_reason -> Anthropic stop_reason
_FINISH_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "end_turn",
}

_XML_INVOKE_RE = re.compile(
    r"(?P<prefix>.*?)"
    r"(?:\bcall\s*)?"
    r"<invoke\s+name=[\"'](?P<tool>[A-Za-z_][A-Za-z0-9_]*)[\"']\s*>"
    r"(?P<body>.*?)"
    r"</invoke>",
    re.IGNORECASE | re.DOTALL,
)

_XML_PARAMETER_RE = re.compile(
    r"<parameter\s+name=[\"'](?P<name>[^\"']+)[\"']\s*>"
    r"(?P<value>.*?)"
    r"</parameter>",
    re.IGNORECASE | re.DOTALL,
)

_XML_TOOL_ALLOWLIST = {
    "Agent", "AskUserQuestion", "Bash", "Edit", "Glob", "Grep", "LS", "LSP",
    "NotebookEdit", "Read", "Skill", "TaskCreate", "TodoWrite", "WebFetch",
    "WebSearch", "Write",
}


def _xml_unescape(text):
    return (
        (text or "")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&amp;", "&")
    )


def _coerce_xml_parameter_value(value):
    text = _xml_unescape(value).strip()
    low = text.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low == "null":
        return None
    if re.fullmatch(r"-?\d+", text):
        try:
            return int(text)
        except ValueError:
            return text
    if re.fullmatch(r"-?\d+\.\d+", text):
        try:
            return float(text)
        except ValueError:
            return text
    return _xml_unescape(value)


def _extract_xml_invoke_tool(text):
    """Return (prefix_text, tool_name, input_obj) for plain-text XML tool calls."""
    if not isinstance(text, str) or "<invoke" not in text or "</invoke>" not in text:
        return None
    match = _XML_INVOKE_RE.search(text)
    if not match:
        return None
    tool = match.group("tool") or ""
    if tool not in _XML_TOOL_ALLOWLIST:
        return None
    params = {}
    body = match.group("body") or ""
    for param in _XML_PARAMETER_RE.finditer(body):
        name = (param.group("name") or "").strip()
        if name:
            params[name] = _coerce_xml_parameter_value(param.group("value") or "")
    if not params:
        return None
    prefix = (match.group("prefix") or "").rstrip()
    return prefix, tool, params


def _fallback_ask_user_question(raw_text):
    text = (raw_text or "").strip()
    if len(text) > 1000:
        text = text[:1000] + "…"
    if not text:
        text = "模型生成的问题参数无法解析，请选择如何继续。"
    return {
        "header": "问题参数已自动修复",
        "question": text,
        "multiSelect": False,
        "options": [
            {"label": "继续", "description": "按当前方向继续"},
            {"label": "调整", "description": "我会补充修改意见"},
        ],
    }


def _repair_ask_user_question_items(questions):
    changed = False
    fixed = []
    for item in questions:
        if not isinstance(item, dict):
            fixed.append(_fallback_ask_user_question(str(item)))
            changed = True
            continue
        new_item = dict(item)
        question = new_item.get("question")
        if not isinstance(question, str) or not question.strip():
            header = new_item.get("header")
            if isinstance(header, str) and header.strip():
                new_item["question"] = header.strip()
            else:
                new_item["question"] = "请选择一个选项。"
            changed = True
        if not isinstance(new_item.get("options"), list) or not new_item.get("options"):
            new_item["options"] = [
                {"label": "继续", "description": "按当前方向继续"},
                {"label": "调整", "description": "我会补充修改意见"},
            ]
            changed = True
        if "multiSelect" not in new_item:
            new_item["multiSelect"] = False
            changed = True
        fixed.append(new_item)
    return fixed, changed


def _normalize_tool_input(tool_name, input_obj):
    """Normalize known tool schema issues before emitting Anthropic tool_use."""
    if tool_name != "AskUserQuestion":
        return input_obj if isinstance(input_obj, dict) else {}
    if not isinstance(input_obj, dict):
        return {"questions": [_fallback_ask_user_question(str(input_obj))]}
    questions = input_obj.get("questions")
    if isinstance(questions, list):
        fixed_questions, _changed = _repair_ask_user_question_items(questions)
        fixed = dict(input_obj)
        fixed["questions"] = fixed_questions
        return fixed
    if isinstance(questions, str):
        text = questions.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    fixed_questions, _changed = _repair_ask_user_question_items(parsed)
                    fixed = dict(input_obj)
                    fixed["questions"] = fixed_questions
                    return fixed
            except json.JSONDecodeError:
                pass
        fixed = dict(input_obj)
        fixed["questions"] = [_fallback_ask_user_question(text)]
        return fixed
    fixed = dict(input_obj)
    fixed["questions"] = [_fallback_ask_user_question("")]
    return fixed


def _openai_tool_call_to_anthropic_block(tc):
    fn = tc.get("function") or {}
    name = str(fn.get("name") or "").strip()
    if not name:
        return None
    raw_args = fn.get("arguments") or "{}"
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError:
        args = {"_raw": raw_args}
    if not isinstance(args, dict):
        args = {"value": args}
    args = _normalize_tool_input(name, args)
    return {
        "type": "tool_use",
        "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:16]}",
        "name": name,
        "input": args,
    }


def _openai_message_to_anthropic_blocks(message):
    """Convert a complete OpenAI assistant message to normalized Anthropic blocks."""
    content_blocks = []
    text_parts = []
    txt = message.get("content")
    if isinstance(txt, str) and txt:
        text_parts.append(txt)
    elif isinstance(txt, list):
        for part in txt:
            if isinstance(part, dict) and part.get("type") == "text":
                text_parts.append(part.get("text", ""))
    text = "\n".join(p for p in text_parts if p)

    extracted = _extract_xml_invoke_tool(text)
    if extracted:
        prefix, tool_name, input_obj = extracted
        if prefix:
            content_blocks.append({"type": "text", "text": prefix + "\n"})
        content_blocks.append({
            "type": "tool_use",
            "id": f"toolu_{uuid.uuid4().hex[:16]}",
            "name": tool_name,
            "input": _normalize_tool_input(tool_name, input_obj),
        })
        logger.warning(
            "XML_INVOKE normalized openai message tool=%s keys=%s prefix_bytes=%d",
            tool_name, sorted(input_obj.keys()), len(prefix.encode("utf-8")),
        )
    elif text:
        content_blocks.append({"type": "text", "text": text})

    for tc in message.get("tool_calls") or []:
        block = _openai_tool_call_to_anthropic_block(tc)
        if block:
            content_blocks.append(block)
    if not content_blocks:
        content_blocks = [{"type": "text", "text": ""}]
    return content_blocks


def _anthropic_stop_reason_for_message(openai_message, finish_reason):
    if openai_message.get("tool_calls"):
        return "tool_use"
    txt = openai_message.get("content")
    if isinstance(txt, str) and _extract_xml_invoke_tool(txt):
        return "tool_use"
    return _FINISH_MAP.get(finish_reason or "stop", "end_turn")


class OpenAIStreamAccumulator:
    """Accumulate OpenAI ChatCompletion chunks into one complete assistant message."""

    def __init__(self):
        self.content_parts = []
        self.tool_calls = {}
        self.finish_reason = None
        self.usage = None

    def add_chunk(self, chunk):
        if chunk.get("usage"):
            self.usage = chunk.get("usage")
        choices = chunk.get("choices") or []
        if not choices:
            return
        choice = choices[0]
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if isinstance(content, str):
            self.content_parts.append(content)
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            rec = self.tool_calls.setdefault(idx, {
                "id": None,
                "type": "function",
                "function": {"name": "", "arguments": ""},
            })
            if tc.get("id"):
                rec["id"] = tc.get("id")
            fn = tc.get("function") or {}
            if fn.get("name"):
                rec["function"]["name"] = fn.get("name")
            if fn.get("arguments"):
                rec["function"]["arguments"] += fn.get("arguments")
        if choice.get("finish_reason"):
            self.finish_reason = choice.get("finish_reason")

    def to_message(self):
        return self.to_completion().message

    def to_completion(self):
        tool_calls = []
        for idx in sorted(self.tool_calls.keys()):
            rec = self.tool_calls[idx]
            if not rec.get("id"):
                rec["id"] = f"call_{uuid.uuid4().hex[:16]}"
            if (rec.get("function") or {}).get("name"):
                tool_calls.append(rec)
            else:
                logger.warning(
                    "Dropping accumulated OpenAI tool_call without name index=%s args_bytes=%d",
                    idx, len((rec.get("function") or {}).get("arguments") or ""),
                )
        message = {"role": "assistant", "content": "".join(self.content_parts)}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return OpenAIChatCompletion(
            id=f"chatcmpl_stream_{uuid.uuid4().hex}",
            message=message,
            finish_reason=self.finish_reason or "stop",
            usage=self.usage or {},
        )


def openai_response_to_anthropic(openai_resp, anthropic_model):
    """Translate a non-streaming OpenAI Chat Completions response into Anthropic Messages."""
    completion = OpenAIChatCompletion.from_raw(openai_resp)
    return _CONVERTER.openai_completion_to_claude(
        completion, anthropic_model,
    ).to_anthropic_dict()


# ── Streaming translator: OpenAI SSE chunks → Anthropic Messages SSE ─────────

def _sse_event(event_name, data_obj):
    """Format a single SSE event as bytes."""
    return (
        f"event: {event_name}\n"
        f"data: {json.dumps(data_obj, ensure_ascii=False)}\n\n"
    ).encode("utf-8")


class AnthropicStreamEmitter:
    """Translate OpenAI/normalized content into Anthropic Messages SSE events.

    Architectural invariant:
      - message_start is emitted once before any content blocks.
      - At most one content block may be open at any time.
      - Every emitted block is start -> delta* -> stop before the next block.
      - Content block indices are globally contiguous from 0 with no gaps.

    OpenAI may stream multiple tool_calls concurrently by `index`; Anthropic
    consumers expect serialized content blocks. Therefore tool_calls must be
    normalized by the converter first, then emitted through `emit_blocks()`.
    """

    def __init__(self, anthropic_model):
        self.model = anthropic_model
        self.message_id = f"msg_{uuid.uuid4().hex}"
        self._started = False
        # Block indices are allocated lazily so the first emitted block always
        # ends up at index 0. Anthropic SDK consumers require content blocks
        # to be numbered contiguously from 0; a gap (e.g. tool_use at index 1
        # when no text block was opened) makes the SDK silently wait forever.
        self._text_open = False
        self._text_index = None
        self._thinking_open = False
        self._thinking_index = None
        self._next_block_index = 0
        self._stop_reason = None
        self._input_tokens = 0
        self._output_tokens = 0

    def emit_message_start(self):
        self._started = True
        return _sse_event("message_start", {
            "type": "message_start",
            "message": {
                "id": self.message_id,
                "type": "message",
                "role": "assistant",
                "model": self.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })

    def _open_text_block(self):
        self._text_open = True
        self._text_index = self._next_block_index
        self._next_block_index += 1
        return _sse_event("content_block_start", {
            "type": "content_block_start",
            "index": self._text_index,
            "content_block": {"type": "text", "text": ""},
        })

    def _close_text_block(self):
        self._text_open = False
        return _sse_event("content_block_stop", {
            "type": "content_block_stop",
            "index": self._text_index,
        })

    def _open_thinking_block(self):
        # Anthropic's `thinking` content block. Claude Code renders it as a
        # collapsible reasoning section in the UI, and importantly it counts
        # as a non-empty assistant message — preventing the
        # "No assistant messages found" failure when an upstream reasoning
        # model spends *all* its output tokens in `reasoning_content`.
        self._thinking_open = True
        self._thinking_index = self._next_block_index
        self._next_block_index += 1
        return _sse_event("content_block_start", {
            "type": "content_block_start",
            "index": self._thinking_index,
            "content_block": {"type": "thinking", "thinking": ""},
        })

    def _close_thinking_block(self):
        self._thinking_open = False
        return _sse_event("content_block_stop", {
            "type": "content_block_stop",
            "index": self._thinking_index,
        })

    def thinking_delta(self, text):
        """Stream a fragment of upstream `delta.reasoning_content`."""
        out = b""
        if not text:
            return out
        # Thinking blocks must come before text/tool_use blocks. We close any
        # open text block on first reasoning fragment so global block order
        # stays well-formed; in practice this only matters if the upstream
        # model streams content and reasoning interleaved.
        if self._text_open:
            out += self._close_text_block()
        if not self._thinking_open:
            out += self._open_thinking_block()
        out += _sse_event("content_block_delta", {
            "type": "content_block_delta",
            "index": self._thinking_index,
            "delta": {"type": "thinking_delta", "thinking": text},
        })
        return out

    def text_delta(self, text):
        out = b""
        if not text:
            return out
        # Reasoning must precede regular content blocks; close any open
        # thinking block before opening a text block.
        if self._thinking_open:
            out += self._close_thinking_block()
        if not self._text_open:
            out += self._open_text_block()
        out += _sse_event("content_block_delta", {
            "type": "content_block_delta",
            "index": self._text_index,
            "delta": {"type": "text_delta", "text": text},
        })
        return out

    def tool_call_delta(self, tc_index, tc_id, name, args_fragment):
        """Deprecated unsafe path.

        OpenAI native tool_calls can be interleaved across indices. Emitting an
        Anthropic tool_use block directly from one delta can overlap content
        blocks and make Claude Code wait until timeout. Callers must aggregate
        tool deltas into normalized blocks and use `emit_blocks()` instead.
        """
        raise RuntimeError(
            "Unsafe incremental tool_use emission is disabled; "
            "aggregate OpenAI tool_calls and emit via emit_blocks()."
        )

    def set_finish(self, finish_reason, usage=None):
        if finish_reason in {"end_turn", "tool_use", "max_tokens", "stop_sequence"}:
            self._stop_reason = finish_reason
        else:
            self._stop_reason = _FINISH_MAP.get(finish_reason or "stop", "end_turn")
        if usage:
            self._input_tokens = usage.get("prompt_tokens", 0) or 0
            self._output_tokens = usage.get("completion_tokens", 0) or 0

    def emit_blocks(self, content_blocks):
        """Emit normalized Anthropic content blocks in strict global order.

        This is the only safe path for tool_use blocks. It closes any open text
        block first, emits each normalized block as a complete start/delta/stop
        unit, and allocates global contiguous indices only for blocks that are
        actually emitted. Invalid blocks are skipped without creating index gaps.
        """
        out = b""
        for block in content_blocks or []:
            if not isinstance(block, dict):
                logger.warning("Skipping non-dict Anthropic content block: %r", block)
                continue
            typ = block.get("type")
            if typ == "text":
                text = block.get("text", "")
                if not isinstance(text, str):
                    text = str(text)
                if not text:
                    continue
                if self._thinking_open:
                    out += self._close_thinking_block()
                if self._text_open:
                    out += self._close_text_block()
                idx = self._next_block_index
                self._next_block_index += 1
                out += _sse_event("content_block_start", {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {"type": "text", "text": ""},
                })
                out += _sse_event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "text_delta", "text": text},
                })
                out += _sse_event("content_block_stop", {
                    "type": "content_block_stop",
                    "index": idx,
                })
            elif typ == "tool_use":
                name = str(block.get("name") or "").strip()
                if not name:
                    logger.warning("Skipping Anthropic tool_use block with empty name; no index allocated")
                    continue
                if self._thinking_open:
                    out += self._close_thinking_block()
                if self._text_open:
                    out += self._close_text_block()
                idx = self._next_block_index
                self._next_block_index += 1
                input_obj = _normalize_tool_input(name, block.get("input") or {})
                input_json = json.dumps(input_obj, ensure_ascii=False, separators=(",", ":"))
                out += _sse_event("content_block_start", {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {
                        "type": "tool_use",
                        "id": block.get("id") or f"toolu_{uuid.uuid4().hex[:16]}",
                        "name": name,
                        "input": {},
                    },
                })
                out += _sse_event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "input_json_delta", "partial_json": input_json},
                })
                out += _sse_event("content_block_stop", {
                    "type": "content_block_stop",
                    "index": idx,
                })
            else:
                logger.warning("Skipping unsupported Anthropic content block type=%r", typ)
        return out

    def finish(self):
        out = b""
        # Close any still-open content blocks before emitting the
        # message_delta/message_stop terminator. Tool blocks are only emitted
        # via emit_blocks(), which always emits complete start/delta/stop
        # units, so we only need to close text/thinking here.
        if self._text_open:
            out += self._close_text_block()
        if self._thinking_open:
            out += self._close_thinking_block()
        # message_delta with stop_reason
        out += _sse_event("message_delta", {
            "type": "message_delta",
            "delta": {
                "stop_reason": self._stop_reason or "end_turn",
                "stop_sequence": None,
            },
            "usage": {"output_tokens": self._output_tokens},
        })
        out += _sse_event("message_stop", {"type": "message_stop"})
        return out


class OpenAIToAnthropicStreamConverter:
    """Convert OpenAI ChatCompletion chunks to Anthropic SSE safely.

    Architecture:
      - Text is streamed with a small hold-back window for XML-tool detection.
      - XML tools are normalized into complete tool_use blocks and emitted via
        `AnthropicStreamEmitter.emit_blocks()`, which serializes and closes them.
      - Native OpenAI tool_calls are never emitted incrementally. OpenAI streams
        tool_calls concurrently/interleaved by `index`, which cannot be mapped
        1:1 to Anthropic's strictly serialized content blocks. We aggregate them
        into a canonical per-index IR and flush them at finish in deterministic
        index order through the same safe `emit_blocks()` path.
    """

    _TEXT_HOLD_CHARS = 128
    _XML_BUFFER_MAX_CHARS = 256 * 1024

    def __init__(self, emitter, request_id=None, model=None, reasoning_mode=None):
        self.emitter = emitter
        self.request_id = request_id or f"stream-{uuid.uuid4().hex[:8]}"
        self.model = model or getattr(emitter, "model", None)
        setattr(self.emitter, "request_id", self.request_id)
        setattr(self.emitter, "diagnostic_model", self.model)
        # How to surface upstream `delta.reasoning_content`. Resolved by the
        # caller from the (provider, model_series) compat profile.
        # See _STANDARD_CHAT_PROFILE / _REASONING_PROFILE / _THINKING_CHAT_PROFILE.
        self.reasoning_mode = (reasoning_mode or "drop").strip().lower()
        if self.reasoning_mode not in {"thinking", "text", "drop"}:
            self.reasoning_mode = "drop"
        self.pending_text = ""
        self.xml_buffer = None
        self.finish_reason = None
        self.usage = None
        self.text_bytes = 0
        self.reasoning_bytes = 0
        self.reasoning_delta_count = 0
        self.tool_names = []
        self.xml_tool_names = []
        self.chunk_count = 0
        self.malformed_chunks = 0
        self._xml_counter = 0
        self.tool_arg_bytes = {}
        self.tool_delta_count = {}
        self.text_delta_count = 0
        self.text_flush_count = 0
        self.output_event_bytes = 0
        # Canonical IR for native OpenAI tool_calls. Keyed only by OpenAI index
        # because id/name/arguments may arrive in different chunks and multiple
        # indices may be interleaved in the same upstream stream.
        self._native_tool_calls = {}

    def feed_chunk(self, chunk):
        self.chunk_count += 1
        out = b""
        if chunk.get("usage"):
            self.usage = chunk.get("usage")
        choices = chunk.get("choices") or []
        if not choices:
            return out
        choice = choices[0]
        delta = choice.get("delta") or {}

        tool_calls = delta.get("tool_calls") or []
        if tool_calls:
            out += self._flush_pending_text(force=True)
            out += self._flush_xml_buffer(force=True)
            for tc in tool_calls:
                idx = tc.get("index", 0)
                fn = tc.get("function") or {}
                name = fn.get("name")
                args_fragment = fn.get("arguments") or ""
                if name and name not in self.tool_names:
                    self.tool_names.append(name)
                arg_len = len(args_fragment.encode("utf-8"))
                key = idx if idx is not None else 0
                self.tool_arg_bytes[key] = self.tool_arg_bytes.get(key, 0) + arg_len
                self.tool_delta_count[key] = self.tool_delta_count.get(key, 0) + 1
                if name or arg_len:
                    logger.info(
                        "STREAM_CONVERT tool_delta request_id=%s model=%s chunk=%d index=%s id=%s name=%s arg_bytes=%d total_arg_bytes=%d",
                        self.request_id, self.model, self.chunk_count, idx, tc.get("id"), name,
                        arg_len, self.tool_arg_bytes[key],
                    )
                self._accumulate_native_tool_call(
                    idx,
                    tc.get("id"),
                    name,
                    args_fragment,
                    self.chunk_count,
                )

        content = delta.get("content")
        if isinstance(content, str) and content:
            out += self._feed_text(content)

        # Handle the `delta.reasoning_content` extension (deepseek-reasoner /
        # deepseek-v4-pro / glm thinking / kimi-k2-thinking / o-series via
        # some gateways / ...). Some providers also use the legacy
        # `delta.reasoning` field — accept either.
        reasoning_chunk = delta.get("reasoning_content")
        if not isinstance(reasoning_chunk, str) or not reasoning_chunk:
            legacy = delta.get("reasoning")
            if isinstance(legacy, str) and legacy:
                reasoning_chunk = legacy
        if isinstance(reasoning_chunk, str) and reasoning_chunk:
            self.reasoning_bytes += len(reasoning_chunk.encode("utf-8"))
            self.reasoning_delta_count += 1
            if self.reasoning_mode == "thinking":
                # Flush any pending text/xml first so global block order is
                # well-formed when reasoning arrives mid-stream (rare).
                out += self._flush_pending_text(force=True)
                out += self._flush_xml_buffer(force=True)
                out += self.emitter.thinking_delta(reasoning_chunk)
            elif self.reasoning_mode == "text":
                # Emit reasoning as visible text wrapped for non-Claude clients.
                out += self._feed_text(reasoning_chunk)
            # "drop" → silently discard (legacy behavior).

        if choice.get("finish_reason"):
            self.finish_reason = choice.get("finish_reason")
        return out

    def finish(self):
        out = b""
        out += self._flush_xml_buffer(force=True)
        out += self._flush_pending_text(force=True)
        native_tool_blocks = self._native_tool_blocks()
        if native_tool_blocks:
            piece = self.emitter.emit_blocks(native_tool_blocks)
            self.output_event_bytes += len(piece)
            out += piece
        effective_finish = self.finish_reason or "stop"
        if self.tool_names and effective_finish in {"stop", "end_turn"}:
            effective_finish = "tool_calls"
        self.emitter.set_finish(effective_finish, self.usage)
        finish_piece = self.emitter.finish()
        self.output_event_bytes += len(finish_piece)
        out += finish_piece
        final_bytes = len(out)
        logger.info(
            "STREAM_CONVERT finish request_id=%s model=%s effective_finish=%s raw_finish=%s final_bytes=%d summary=%s",
            self.request_id, self.model, effective_finish, self.finish_reason, final_bytes, self.summary(),
        )
        return out

    def summary(self):
        return {
            "request_id": self.request_id,
            "chunks": self.chunk_count,
            "text_bytes": self.text_bytes,
            "reasoning_bytes": self.reasoning_bytes,
            "reasoning_delta_count": self.reasoning_delta_count,
            "reasoning_mode": self.reasoning_mode,
            "tool_names": self.tool_names,
            "xml_tool_names": self.xml_tool_names,
            "tool_arg_bytes": dict(self.tool_arg_bytes),
            "tool_delta_count": dict(self.tool_delta_count),
            "text_delta_count": self.text_delta_count,
            "text_flush_count": self.text_flush_count,
            "output_event_bytes": self.output_event_bytes,
            "pending_text_bytes": len(self.pending_text.encode("utf-8")),
            "xml_buffer_bytes": len(self.xml_buffer.encode("utf-8")) if self.xml_buffer else 0,
            "finish_reason": self.finish_reason,
            "usage": self.usage,
            "malformed_chunks": self.malformed_chunks,
            "native_tool_ir_count": len(self._native_tool_calls),
        }

    def _accumulate_native_tool_call(self, idx, tc_id, name, args_fragment, chunk_no):
        """Accumulate one OpenAI tool_call delta into canonical per-index IR.

        This is intentionally non-emitting. OpenAI-compatible providers may
        interleave multiple tool_call indices in one stream, while Anthropic SSE
        content blocks are strictly serial. The only durable identity during
        streaming is OpenAI's `index`; id/name/arguments are completed lazily.
        """
        key = idx if idx is not None else 0
        rec = self._native_tool_calls.get(key)
        if rec is None:
            rec = {
                "id": None,
                "name": "",
                "arguments": "",
                "first_chunk": chunk_no,
                "last_chunk": chunk_no,
            }
            self._native_tool_calls[key] = rec
        rec["last_chunk"] = chunk_no
        if tc_id and not rec["id"]:
            rec["id"] = tc_id
        if name and not rec["name"]:
            rec["name"] = name
        if args_fragment:
            rec["arguments"] += args_fragment

    def _native_tool_blocks(self):
        """Return normalized Anthropic tool_use blocks from native tool IR."""
        blocks = []
        if not self._native_tool_calls:
            return blocks
        for key in sorted(self._native_tool_calls.keys(), key=lambda item: (str(type(item)), item)):
            rec = self._native_tool_calls[key]
            name = str(rec.get("name") or "").strip()
            raw_args = rec.get("arguments") or "{}"
            if not name:
                logger.warning(
                    "Dropping native OpenAI tool_call without name request_id=%s model=%s index=%s args_bytes=%d first_chunk=%s last_chunk=%s",
                    self.request_id, self.model, key, len(raw_args.encode("utf-8")),
                    rec.get("first_chunk"), rec.get("last_chunk"),
                )
                continue
            try:
                input_obj = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                input_obj = {"_raw": raw_args}
                logger.warning(
                    "Native tool_call arguments are not JSON request_id=%s model=%s index=%s name=%s args_bytes=%d",
                    self.request_id, self.model, key, name, len(raw_args.encode("utf-8")),
                )
            if not isinstance(input_obj, dict):
                input_obj = {"value": input_obj}
            input_obj = _normalize_tool_input(name, input_obj)
            blocks.append({
                "type": "tool_use",
                "id": rec.get("id") or f"toolu_{uuid.uuid4().hex[:16]}",
                "name": name,
                "input": input_obj,
            })
        logger.info(
            "STREAM_CONVERT native_tool_flush request_id=%s model=%s blocks=%d indices=%s",
            self.request_id, self.model, len(blocks), sorted(self._native_tool_calls.keys(), key=lambda item: (str(type(item)), item)),
        )
        return blocks

    def _feed_text(self, text):
        if self.xml_buffer is not None:
            self.xml_buffer += text
            return self._process_xml_buffer()

        self.pending_text += text
        return self._process_pending_text()

    def _process_pending_text(self):
        out = b""
        start = self._find_xml_candidate_start(self.pending_text)
        if start is not None:
            prefix = self.pending_text[:start]
            if prefix:
                out += self._emit_text(prefix)
            self.xml_buffer = self.pending_text[start:]
            self.pending_text = ""
            out += self._process_xml_buffer()
            return out

        if len(self.pending_text) > self._TEXT_HOLD_CHARS:
            safe = self.pending_text[:-self._TEXT_HOLD_CHARS]
            self.pending_text = self.pending_text[-self._TEXT_HOLD_CHARS:]
            logger.info(
                "STREAM_CONVERT text_flush request_id=%s model=%s mode=safe_window bytes=%d hold_bytes=%d chunk=%d",
                self.request_id, self.model, len(safe.encode("utf-8")),
                len(self.pending_text.encode("utf-8")), self.chunk_count,
            )
            out += self._emit_text(safe)
        return out

    def _process_xml_buffer(self):
        if self.xml_buffer is None:
            return b""
        if "</invoke>" not in self.xml_buffer:
            if len(self.xml_buffer) > self._XML_BUFFER_MAX_CHARS:
                logger.warning(
                    "XML invoke buffer exceeded %d chars; releasing as text",
                    self._XML_BUFFER_MAX_CHARS,
                )
                return self._flush_xml_buffer(force=True)
            return b""

        extracted = _extract_xml_invoke_tool(self.xml_buffer)
        if not extracted:
            return self._flush_xml_buffer(force=True)
        match = _XML_INVOKE_RE.search(self.xml_buffer)
        suffix = self.xml_buffer[match.end():] if match else ""

        prefix, tool_name, input_obj = extracted
        out = b""
        if prefix:
            out += self._emit_text(prefix + "\n")
        input_obj = _normalize_tool_input(tool_name, input_obj)
        self._xml_counter += 1
        tool_id = f"toolu_xml_{uuid.uuid4().hex[:16]}"
        piece = self.emitter.emit_blocks([{
            "type": "tool_use",
            "id": tool_id,
            "name": tool_name,
            "input": input_obj,
        }])
        self.output_event_bytes += len(piece)
        out += piece
        if tool_name not in self.tool_names:
            self.tool_names.append(tool_name)
        self.xml_tool_names.append(tool_name)
        logger.warning(
            "XML_INVOKE stream-converted tool=%s keys=%s input_bytes=%d",
            tool_name, sorted(input_obj.keys()), len(json.dumps(input_obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
        )
        self.xml_buffer = None
        if suffix:
            out += self._feed_text(suffix)
        return out

    def _flush_pending_text(self, force=False):
        if not self.pending_text:
            return b""
        text = self.pending_text
        self.pending_text = ""
        logger.info(
            "STREAM_CONVERT text_flush request_id=%s model=%s mode=force bytes=%d chunk=%d",
            self.request_id, self.model, len(text.encode("utf-8")), self.chunk_count,
        )
        return self._emit_text(text)

    def _flush_xml_buffer(self, force=False):
        if not self.xml_buffer:
            self.xml_buffer = None
            return b""
        text = self.xml_buffer
        self.xml_buffer = None
        logger.warning(
            "STREAM_CONVERT xml_fallback request_id=%s model=%s bytes=%d chunk=%d",
            self.request_id, self.model, len(text.encode("utf-8")), self.chunk_count,
        )
        return self._emit_text(text)

    def _emit_text(self, text):
        if not text:
            return b""
        self.text_bytes += len(text.encode("utf-8"))
        self.text_delta_count += 1
        self.text_flush_count += 1
        out = self.emitter.text_delta(text)
        self.output_event_bytes += len(out)
        return out

    def _find_xml_candidate_start(self, text):
        invoke_idx = text.find("<invoke")
        if invoke_idx < 0:
            return None
        # If the model emitted `call\n<invoke...`, keep the marker in the
        # buffered XML so _extract_xml_invoke_tool can strip it cleanly.
        call_idx = text.rfind("call", 0, invoke_idx)
        if call_idx >= 0 and text[call_idx + 4:invoke_idx].strip() == "":
            return call_idx
        return invoke_idx


class _DownstreamGoneError(Exception):
    def __init__(self, phase, cause):
        super().__init__(f"downstream client gone (phase={phase}): {cause}")
        self.phase = phase
        self.cause = cause


class ProxyHTTPRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    # ── Helpers ──────────────────────────────────────────────────────────

    def _send_json_response(self, status_code, data, extra_headers=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_error_response(self, status_code, error_type, message):
        self._send_json_response(status_code, {
            "type": "error",
            "error": {"type": error_type, "message": message},
        })

    def _get_request_body(self):
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 0:
            return self.rfile.read(content_length)
        return b""

    # ── Routing ──────────────────────────────────────────────────────────

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/v1/models":
            return self._handle_list_models()
        if path == "/admin/reload":
            return self._handle_admin_reload()
        if path == "/admin/health":
            return self._send_json_response(200, {"status": "ok", "loaded": is_loaded()})
        self._send_error_response(404, "not_found_error", f"Unknown path: {path}")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/v1/messages":
            return self._handle_messages()
        if path == "/v1/messages/count_tokens":
            return self._handle_count_tokens()
        if path == "/admin/reload":
            return self._handle_admin_reload()
        self._send_error_response(404, "not_found_error", f"Unknown path: {path}")

    # ── Handlers ─────────────────────────────────────────────────────────

    def _handle_list_models(self):
        if not is_loaded():
            self._send_error_response(503, "api_error", "Proxy configuration not loaded")
            return
        resp = build_models_response()
        logger.info("GET /v1/models -> 200 OK (%d model(s))", len(resp["data"]))
        self._send_json_response(200, resp)

    def _handle_admin_reload(self):
        ok = load_config()
        if not ok:
            logger.error("Admin reload -> 500 ERROR")
            return self._send_json_response(500, {
                "status": "error",
                "message": f"Failed to reload config from {_config_path}",
            })
        sync_ok, sync_info = sync_claude_settings()
        resp = {
            "status": "ok" if sync_ok else "partial",
            "models": list(get_config().keys()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "claude_settings_sync": {"ok": sync_ok, **sync_info},
        }
        logger.info("Admin reload -> 200 (%s)", resp["status"])
        self._send_json_response(200, resp)

    def _resolve_model(self, body_obj):
        if not is_loaded():
            return None, None, "Proxy configuration not loaded"
        model = body_obj.get("model")
        if not model:
            return None, None, "Missing 'model' field in request body"
        cfg = get_model_config(model)
        if not cfg:
            return None, None, f"Model '{model}' is not available in proxy configuration"
        return model, cfg, None

    def _handle_count_tokens(self):
        """Provide a *very* rough token count using an OpenAI-style heuristic.

        We don't have access to the upstream's tokenizer over plain HTTP, and
        tiktoken is not necessarily installed. Returning a heuristic is good
        enough to keep Claude Code's UI happy.
        """
        try:
            body = self._get_request_body()
            req = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return self._send_error_response(400, "invalid_request_error", "Invalid JSON body")
        model, _cfg, err = self._resolve_model(req)
        if err:
            return self._send_error_response(400, "invalid_request_error", err)

        # Heuristic: ~4 characters per token.
        text_chars = 0
        for m in req.get("messages") or []:
            text_chars += len(_flatten_anthropic_text(m.get("content")))
        sys_field = req.get("system")
        if sys_field:
            text_chars += len(_flatten_anthropic_text(
                sys_field if isinstance(sys_field, list) else [{"type": "text", "text": sys_field}]
            ))
        approx = max(1, text_chars // 4)
        logger.info("count_tokens model=%s approx=%d (chars=%d)", model, approx, text_chars)
        self._send_json_response(200, {"input_tokens": approx})

    def _handle_messages(self):
        try:
            raw_body = self._get_request_body()
            req = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            return self._send_error_response(400, "invalid_request_error", "Invalid JSON body")

        model, cfg, err = self._resolve_model(req)
        if err:
            return self._send_error_response(400, "invalid_request_error", err)

        upstream_base = cfg["OPENAI_BASE_URL"].rstrip("/")
        upstream_path = "/chat/completions"
        upstream_model = cfg.get("upstream_model") or model
        api_key = cfg["OPENAI_API_KEY"]
        is_stream = bool(req.get("stream", False))
        request_id = f"o2c_{uuid.uuid4().hex[:10]}"

        openai_req = anthropic_request_to_openai(req, upstream_model, cfg)
        body_bytes = json.dumps(openai_req, ensure_ascii=False).encode("utf-8")

        # Pass through the client's original headers (User-Agent, X-Request-Id,
        # tracing headers, custom gateway headers, etc.) and only override the
        # ones we MUST control. Many internal gateways (incl. wanqing) bucket
        # rate-limits by client identity headers — if we strip them and send a
        # bare "Python-urllib/..." UA, the request lands in the strictest
        # anonymous bucket and gets 429'd far more aggressively. claude_proxy
        # learned the same lesson; mirror its approach here for consistency.
        skip_headers = {
            "host",
            "x-api-key",            # Anthropic auth, replaced by Authorization
            "authorization",        # we set our own Bearer below
            "content-length",       # recomputed
            "content-type",         # we always send JSON to OpenAI
            "accept",               # we set SSE/JSON below
            "connection",
            "transfer-encoding",
            "accept-encoding",      # avoid gzip-on-SSE stalls (see claude_proxy)
        }
        headers = {}
        for k, v in (self.headers.items() if hasattr(self.headers, "items") else []):
            if k.lower() not in skip_headers:
                headers[k] = v
        headers["Authorization"] = f"Bearer {api_key}"
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "text/event-stream" if is_stream else "application/json"
        headers["Accept-Encoding"] = "identity"
        # OpenAI returns usage in stream when stream_options.include_usage=True
        if is_stream:
            openai_req["stream_options"] = {"include_usage": True}
            body_bytes = json.dumps(openai_req, ensure_ascii=False).encode("utf-8")
        headers["Content-Length"] = str(len(body_bytes))

        flags = _resolve_compat_flags(cfg)
        logger.info(
            "Forwarding /v1/messages request_id=%s model=%s upstream=%s%s upstream_model=%s "
            "provider=%s%s model_series=%s%s max_field=%s stream=%s messages=%d tools=%d",
            request_id, model, upstream_base, upstream_path, upstream_model,
            flags["provider"], "" if flags["known_provider"] else " (unknown→standard)",
            flags["model_series"] or "<default>",
            "" if (not flags["model_series"] or flags["known_model_series"]) else " (unknown)",
            flags["max_tokens_field"],
            is_stream,
            len(req.get("messages") or []),
            len(req.get("tools") or []),
        )

        try:
            if is_stream:
                self._handle_streaming(request_id, model, upstream_base, upstream_path, headers, body_bytes,
                                       reasoning_mode=flags.get("reasoning_mode", "drop"))
            else:
                self._handle_non_streaming(model, upstream_base, upstream_path, headers, body_bytes)
        except _DownstreamGoneError as e:
            logger.warning("Downstream client gone (phase=%s): %s", e.phase, e.cause)
        except Exception as e:
            logger.error("Error proxying /v1/messages: %s", e, exc_info=True)
            try:
                self._send_error_response(502, "api_error", f"Upstream request failed: {e}")
            except (BrokenPipeError, ConnectionResetError):
                pass

    # ── Non-streaming path ──────────────────────────────────────────────

    def _handle_non_streaming(self, model, upstream_base, upstream_path, headers, body_bytes):
        status, resp_headers, resp_body = forward_openai_request(
            upstream_base, upstream_path, headers, body_bytes, stream=False,
        )
        if status >= 400:
            logger.error("Upstream returned %d: %s", status, resp_body[:500])
            try:
                err = json.loads(resp_body or b"{}")
            except json.JSONDecodeError:
                err = {"error": {"message": resp_body[:500].decode("utf-8", errors="replace")}}
            msg = (err.get("error") or {}).get("message", f"Upstream error {status}")
            return self._send_error_response(status, "api_error", msg)

        try:
            openai_resp = json.loads(resp_body)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse upstream JSON: %s; body[:500]=%s", e, resp_body[:500])
            return self._send_error_response(502, "api_error", "Upstream returned non-JSON body")

        anthropic_resp = openai_response_to_anthropic(openai_resp, model)
        try:
            self._send_json_response(200, anthropic_resp)
        except (BrokenPipeError, ConnectionResetError) as e:
            raise _DownstreamGoneError("write_response", e) from e

    # ── Streaming path ──────────────────────────────────────────────────

    def _handle_streaming(self, request_id, model, upstream_base, upstream_path, headers, body_bytes,
                           reasoning_mode="drop"):
        # First attempt's upstream fetch is done up-front so a 4xx can be
        # forwarded as a non-stream HTTP error (we haven't sent SSE headers
        # yet). Subsequent retry attempts (for empty-stop turns) re-open the
        # upstream from inside the retry loop below.
        status, resp_headers, response, conn = forward_openai_request(
            upstream_base, upstream_path, headers, body_bytes, stream=True,
        )
        if status >= 400:
            try:
                err_preview = response.read(2048)
            except Exception:
                err_preview = b""
            logger.error("Upstream stream error %d: %s", status, err_preview[:500])
            try:
                self._send_error_response(status, "api_error",
                                          err_preview.decode("utf-8", errors="replace") or f"Upstream error {status}")
            finally:
                conn.close()
            return

        # Send headers downstream as Anthropic SSE.
        #
        # IMPORTANT: SSE responses have no Content-Length and we do not emit
        # Transfer-Encoding: chunked, so the only legal way for an HTTP/1.1
        # client to know the body is finished is by the server closing the
        # TCP connection. If we advertise `Connection: keep-alive`, modern
        # SDKs (httpx/anthropic, undici, etc.) will return the socket to
        # their connection pool after `message_stop`, then send the *next*
        # request on the same socket — but BaseHTTPRequestHandler is
        # single-shot per connection in our threaded server, so that next
        # request is silently never read, and the client only notices after
        # its own multi-minute idle timeout. That is exactly what we saw:
        # subagent finishes a tool, sends the follow-up /v1/messages, and
        # 4 minutes later Claude Code reports "API Error: The operation
        # timed out." while the proxy logs no incoming request at all.
        #
        # Mark every SSE response as `Connection: close` and force the
        # handler to close the socket after writing, so the client SDK
        # opens a fresh connection for the next turn. Anthropic's own
        # Messages API streams the same way.
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            # Tell BaseHTTPRequestHandler not to try to read another request
            # on this socket after we return.
            self.close_connection = True
        except (BrokenPipeError, ConnectionResetError) as e:
            try:
                conn.close()
            except Exception:
                pass
            raise _DownstreamGoneError("write_headers", e) from e

        emitter = AnthropicStreamEmitter(model)
        stream_converter = OpenAIToAnthropicStreamConverter(
            emitter, request_id=request_id, model=model, reasoning_mode=reasoning_mode,
        )

        last_downstream_write_at = time.monotonic()
        keepalive_sent = 0

        def _write(buf):
            nonlocal last_downstream_write_at
            if not buf:
                return
            try:
                self.wfile.write(buf)
                self.wfile.flush()
                last_downstream_write_at = time.monotonic()
            except (BrokenPipeError, ConnectionResetError) as e:
                raise _DownstreamGoneError("write_stream", e) from e

        # Anthropic-format SSE keep-alive. Real api.anthropic.com sends
        #   event: ping
        #   data: {"type":"ping"}
        # whenever the upstream has been silent for a while. SDKs that strictly
        # validate event names (httpx + anthropic-sdk-python in particular)
        # treat plain `:keep-alive` comments as inert, but a typed `ping`
        # event resets their per-event idle deadline. Sending pings keeps both
        # any intermediate proxies (nginx, ALB, …) and the SDK happy.
        def _send_downstream_keepalive_if_due(now=None):
            nonlocal keepalive_sent
            if _stream_keepalive <= 0:
                return
            now = now if now is not None else time.monotonic()
            if now - last_downstream_write_at < _stream_keepalive:
                return
            _write(b"event: ping\ndata: {\"type\":\"ping\"}\n\n")
            keepalive_sent += 1

        # Emit Anthropic message_start *once* across all retry attempts. The
        # emitter/converter state is reused: empty-stop retries do not produce
        # any content blocks, so reusing the same emitter keeps block indices
        # consistent for the eventual non-empty turn.
        _write(emitter.emit_message_start())

        attempt = 0
        max_attempts = 1 + max(0, _empty_stop_max_retry)
        bytes_streamed = 0
        line_count = 0
        finish_reason = None
        upstream_usage = None
        exit_reason = "unknown"
        client_gone = False

        # Holders so the outer `finally` can close whatever upstream is
        # currently open after the loop terminates (success, retry-exhaust,
        # error, or client-gone).
        active_response = response
        active_conn = conn

        try:
            while True:
                attempt += 1
                # Per-attempt counters/state — block indices in the emitter
                # remain monotonic across attempts.
                stream_converter.finish_reason = None
                stream_converter.usage = stream_converter.usage  # keep last known
                last_data_at = time.monotonic()
                attempt_lines = 0
                attempt_bytes = 0

                # Background reader thread (same pattern as claude_proxy)
                line_queue: "queue.Queue" = queue.Queue(maxsize=1024)
                reader_stop = threading.Event()
                _resp_for_reader = active_response

                def _reader(_resp=_resp_for_reader, _q=line_queue, _stop=reader_stop):
                    try:
                        while not _stop.is_set():
                            try:
                                line = _resp.readline()
                            except Exception as exc:
                                _q.put(("err", exc))
                                return
                            if not line:
                                _q.put(("eof", None))
                                return
                            try:
                                _q.put(("data", line), timeout=5)
                            except queue.Full:
                                _q.put(("err", RuntimeError("downstream queue full")))
                                return
                    except Exception as exc:
                        try:
                            _q.put_nowait(("err", exc))
                        except Exception:
                            pass

                reader_thread = threading.Thread(target=_reader, name="openai-stream-reader", daemon=True)
                reader_thread.start()

                attempt_exit = "unknown"
                try:
                    while True:
                        try:
                            kind, payload = line_queue.get(timeout=1.0)
                        except queue.Empty:
                            kind = "tick"
                            payload = None

                        if kind == "data":
                            raw_line = payload
                            attempt_bytes += len(raw_line)
                            attempt_lines += 1
                            last_data_at = time.monotonic()
                            try:
                                _send_downstream_keepalive_if_due(last_data_at)
                            except _DownstreamGoneError:
                                client_gone = True
                                exit_reason = "client_gone_on_keepalive"
                                raise

                            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                            if not line:
                                continue
                            if not line.startswith("data:"):
                                continue
                            data_str = line[len("data:"):].strip()
                            if data_str == "[DONE]":
                                continue
                            try:
                                chunk = json.loads(data_str)
                            except json.JSONDecodeError:
                                logger.warning("Skipping malformed SSE chunk request_id=%s: %s", request_id, data_str[:200])
                                continue

                            try:
                                out = stream_converter.feed_chunk(chunk)
                                if out:
                                    _write(out)
                            except Exception as exc:
                                logger.error("OpenAI stream conversion failed: %s", exc, exc_info=True)
                                raise
                            if stream_converter.usage:
                                upstream_usage = stream_converter.usage
                            if stream_converter.finish_reason:
                                finish_reason = stream_converter.finish_reason
                            continue

                        if kind == "eof":
                            idle_before_eof = time.monotonic() - last_data_at
                            logger.info(
                                "Upstream EOF request_id=%s attempt=%d after %d lines / %d bytes (finish=%s); "
                                "idle_before_eof=%.2fs",
                                request_id, attempt, attempt_lines, attempt_bytes, finish_reason, idle_before_eof,
                            )
                            attempt_exit = "upstream_eof"
                            break

                        if kind == "err":
                            exc = payload
                            msg = str(exc)
                            if "timed out" in msg.lower() or isinstance(exc, TimeoutError):
                                logger.error(
                                    "Upstream stream read timed out request_id=%s attempt=%d "
                                    "(streamed %d lines / %d bytes, %d keep-alives sent). "
                                    "exc_type=%s",
                                    request_id, attempt, attempt_lines, attempt_bytes, keepalive_sent,
                                    type(exc).__name__,
                                )
                                attempt_exit = "upstream_read_timeout"
                            else:
                                logger.error(
                                    "Upstream stream read error request_id=%s attempt=%d after %d lines / %d bytes "
                                    "(%d keep-alives sent). exc_type=%s msg=%s",
                                    request_id, attempt, attempt_lines, attempt_bytes, keepalive_sent,
                                    type(exc).__name__, msg,
                                    exc_info=True,
                                )
                                attempt_exit = "upstream_read_error"
                            break

                        # tick
                        now = time.monotonic()
                        if now - last_data_at > _stream_timeout:
                            logger.error(
                                "Upstream stream idle request_id=%s attempt=%d >%ds after %d lines / %d bytes "
                                "(%d keep-alives sent). Closing.",
                                request_id, attempt, _stream_timeout, attempt_lines, attempt_bytes, keepalive_sent,
                            )
                            attempt_exit = "stream_timeout"
                            break

                        # Proactive idle-limit guard (only when configured > 0).
                        if (
                            _upstream_idle_limit > 0
                            and now - last_data_at > _upstream_idle_limit
                        ):
                            logger.warning(
                                "Upstream silent request_id=%s attempt=%d >%ds (no data, no ping). "
                                "Proactively closing stream "
                                "(streamed %d lines / %d bytes, %d keep-alives sent).",
                                request_id, attempt,
                                _upstream_idle_limit,
                                attempt_lines, attempt_bytes, keepalive_sent,
                            )
                            try:
                                err_payload = json.dumps({
                                    "type": "error",
                                    "error": {
                                        "type": "upstream_idle_timeout",
                                        "message": (
                                            f"Upstream gateway stopped sending data for "
                                            f"{_upstream_idle_limit}s. The request was "
                                            f"closed by the proxy. Please retry."
                                        ),
                                    },
                                }, ensure_ascii=False)
                                _write(b"event: error\n")
                                _write(b"data: " + err_payload.encode("utf-8") + b"\n\n")
                                stop_payload = json.dumps({"type": "message_stop"})
                                _write(b"event: message_stop\n")
                                _write(b"data: " + stop_payload.encode("utf-8") + b"\n\n")
                            except _DownstreamGoneError:
                                client_gone = True
                            except Exception as e:
                                logger.warning("Failed to write synthetic stop event: %s", e)
                            attempt_exit = "upstream_idle_limit"
                            break

                        if _stream_keepalive > 0:
                            try:
                                _send_downstream_keepalive_if_due(now)
                            except _DownstreamGoneError:
                                client_gone = True
                                exit_reason = "client_gone_on_keepalive"
                                raise
                finally:
                    reader_stop.set()

                bytes_streamed += attempt_bytes
                line_count += attempt_lines

                # Decide whether to retry this attempt. Two retry-worthy
                # cases:
                #
                #   (A) "Hard empty turn": upstream cleanly closed finish=stop
                #       and produced literally nothing — no text, no
                #       reasoning, no tool calls, completion_tokens<=1.
                #       Original P0 case.
                #
                #   (B) "Reasoning-only dead-end" (thinking-mode gateways like
                #       deepseek-v4-pro / glm-thinking / kimi-k2-thinking):
                #       finish=stop with non-zero reasoning_bytes but ZERO
                #       client-actionable output (no text + no tool_calls).
                #       Claude Code cannot make progress on a turn that only
                #       contains a `thinking` block — its agentic loop stalls
                #       and the user perceives the session as "stuck". The
                #       upstream burned tokens reasoning but never decided to
                #       speak/act. A fresh attempt usually breaks out of this
                #       loop because the prior reasoning is now part of the
                #       conversation context (echoed via reasoning_content),
                #       so the model has more signal to commit to an answer
                #       or tool call.
                #
                # Anything else (errors, timeouts, tool_calls present, real
                # visible text) falls through to finalization.
                visible_text = stream_converter.text_bytes
                visible_reasoning = stream_converter.reasoning_bytes
                visible_tools = list(stream_converter.tool_names)
                stopped = (
                    attempt_exit == "upstream_eof"
                    and (stream_converter.finish_reason or finish_reason) == "stop"
                )
                completion_tokens = 0
                if isinstance(stream_converter.usage, dict):
                    completion_tokens = int(stream_converter.usage.get("completion_tokens") or 0)
                hard_empty_turn = (
                    stopped
                    and visible_text == 0
                    and visible_reasoning == 0
                    and not visible_tools
                    and completion_tokens <= 1
                )
                # Reasoning-only retries are expensive (each attempt burns
                # tokens on more reasoning) and rarely converge past attempt
                # 2. Cap at 1 retry independent of empty_stop_max_retry so a
                # genuinely "only-thinking" upstream doesn't 3x our latency
                # and bill before we fall through to the fallback emitter.
                reasoning_only_turn = (
                    stopped
                    and visible_text == 0
                    and not visible_tools
                    and visible_reasoning > 0
                    and (stream_converter.reasoning_mode or "drop") == "thinking"
                    and attempt <= 1
                )
                empty_turn = hard_empty_turn or reasoning_only_turn

                if empty_turn and attempt < max_attempts and not client_gone:
                    logger.warning(
                        "STREAM_CONVERT empty_stop_retry request_id=%s attempt=%d/%d "
                        "completion_tokens=%d — re-issuing upstream request",
                        request_id, attempt, max_attempts, completion_tokens,
                    )
                    # Close the now-empty upstream connection and back off a
                    # tiny bit before re-sending. We deliberately keep using
                    # the same emitter/converter so the eventual non-empty
                    # turn still appears to the client as a single message.
                    try:
                        active_conn.close()
                    except Exception:
                        pass
                    time.sleep(min(2.0, _DEFAULT_EMPTY_STOP_BASE_DELAY * (2 ** (attempt - 1))))
                    # Reset converter usage/finish so the next attempt's
                    # stop-detection looks at fresh upstream state.
                    stream_converter.usage = None
                    stream_converter.finish_reason = None
                    try:
                        new_status, _new_headers, new_response, new_conn = forward_openai_request(
                            upstream_base, upstream_path, headers, body_bytes, stream=True,
                        )
                    except Exception as e:
                        logger.error(
                            "empty_stop_retry forward failed request_id=%s attempt=%d: %s",
                            request_id, attempt, e,
                        )
                        exit_reason = "empty_stop_retry_failed"
                        break
                    if new_status >= 400:
                        try:
                            err_preview = new_response.read(2048)
                        except Exception:
                            err_preview = b""
                        logger.error(
                            "empty_stop_retry got %d body[:200]=%s — surrendering",
                            new_status, err_preview[:200],
                        )
                        try:
                            new_conn.close()
                        except Exception:
                            pass
                        exit_reason = "empty_stop_retry_status_%d" % new_status
                        break
                    active_response = new_response
                    active_conn = new_conn
                    continue

                # Either we got real content, or we ran out of retries, or
                # the attempt failed for a non-empty reason. Carry the last
                # attempt's exit reason out and finalize.
                exit_reason = attempt_exit
                break

            # Reasoning-only dead-end: the upstream burned the entire turn
            # on `reasoning_content` without emitting visible text or any
            # tool_call, then sent finish=stop. We've already exhausted the
            # reasoning_only_turn retry above and it still came back empty.
            #
            # Returning this turn as-is to Claude Code makes the agent loop
            # stall: from its perspective the assistant message contains
            # ONLY a `thinking` block and stop_reason=end_turn, which is a
            # "the model is done" signal — Claude Code waits for the user.
            #
            # Earlier we tried injecting a fallback text block; that just
            # surfaced static "please retry" text to the user, which Claude
            # Code happily treated as the model's answer and *also* stopped.
            #
            # Architecturally correct fix: emit a mid-stream Anthropic
            # `overloaded_error`. The Anthropic SDK (used by Claude Code)
            # raises this as a transient error and Claude Code's HTTP retry
            # layer re-issues the request automatically, exactly like a 529
            # response. The partial `message_start` / thinking blocks we've
            # already written are discarded by the SDK on error. We then
            # SKIP the normal finish() since we're terminating the stream
            # with an error frame.
            reasoning_only_dead_end = False
            try:
                summary_pre = stream_converter.summary()
                if (
                    summary_pre.get("text_bytes", 0) == 0
                    and not summary_pre.get("tool_names")
                    and summary_pre.get("reasoning_bytes", 0) > 0
                    and (stream_converter.finish_reason or finish_reason) == "stop"
                ):
                    reasoning_only_dead_end = True
                    err_payload = json.dumps({
                        "type": "error",
                        "error": {
                            "type": "overloaded_error",
                            "message": (
                                "Upstream model produced only internal "
                                "reasoning with no visible output (reasoning-"
                                "only dead-end). Treat as transient and "
                                "retry."
                            ),
                        },
                    }, ensure_ascii=False)
                    try:
                        _write(b"event: error\n")
                        _write(b"data: " + err_payload.encode("utf-8") + b"\n\n")
                    except _DownstreamGoneError:
                        client_gone = True
                    logger.warning(
                        "STREAM_CONVERT reasoning_only_dead_end request_id=%s "
                        "reasoning_bytes=%d attempts=%d — emitted "
                        "overloaded_error so Claude Code auto-retries",
                        request_id, summary_pre.get("reasoning_bytes", 0),
                        attempt,
                    )
                    exit_reason = "reasoning_only_dead_end"
            except Exception as _fb_e:
                logger.warning(
                    "reasoning_only_dead_end handler failed request_id=%s: %s",
                    request_id, _fb_e,
                )

            if reasoning_only_dead_end:
                # Skip finish() — we ended the stream with `event: error`.
                summary = stream_converter.summary()
                logger.info(
                    "Stream done request_id=%s model=%s lines=%d bytes=%d "
                    "keepalives=%d attempts=%d finish=%s usage=%s "
                    "exit_reason=%s client_gone=%s",
                    request_id, model, line_count, bytes_streamed,
                    keepalive_sent, attempt,
                    stream_converter.finish_reason or finish_reason,
                    stream_converter.usage or upstream_usage,
                    exit_reason, client_gone,
                )
                return

            # Flush any buffered text/XML candidate and finish the Anthropic
            # stream. Text may have been emitted incrementally, but native
            # OpenAI tool_calls are normalized and serialized at finish time to
            # avoid illegal overlapping Anthropic content blocks.
            final_out = stream_converter.finish()
            upstream_usage = stream_converter.usage or upstream_usage
            finish_reason = stream_converter.finish_reason or finish_reason
            if final_out:
                _write(final_out)
            summary = stream_converter.summary()
            usage_for_warn = summary.get("usage") or {}
            completion_details = usage_for_warn.get("completion_tokens_details") or {}
            reasoning_tokens = (
                usage_for_warn.get("reasoning_tokens")
                or completion_details.get("reasoning_tokens")
                or 0
            )
            reasoning_bytes = summary.get("reasoning_bytes", 0) if isinstance(summary, dict) else 0
            reasoning_emitted = (
                reasoning_bytes > 0
                and summary.get("reasoning_mode") == "thinking"
            )
            if (
                summary.get("text_bytes", 0) == 0
                and not summary.get("tool_names")
                and not reasoning_emitted
            ):
                logger.warning(
                    "STREAM_CONVERT empty_visible_output request_id=%s model=%s finish=%s "
                    "reasoning_tokens=%s reasoning_bytes=%s reasoning_mode=%s attempts=%d usage=%s",
                    request_id, model, summary.get("finish_reason"),
                    reasoning_tokens, reasoning_bytes,
                    summary.get("reasoning_mode"), attempt, usage_for_warn,
                )
            logger.info(
                "Converted OpenAI stream incrementally request_id=%s model=%s attempts=%d summary=%s",
                request_id, model, attempt, summary,
            )
            if exit_reason == "unknown":
                exit_reason = "normal"
            logger.info(
                "Stream done request_id=%s model=%s lines=%d bytes=%d keepalives=%d attempts=%d finish=%s "
                "usage=%s exit_reason=%s client_gone=%s",
                request_id, model, line_count, bytes_streamed, keepalive_sent, attempt, finish_reason,
                upstream_usage, exit_reason, client_gone,
            )
        finally:
            try:
                active_conn.close()
            except Exception:
                pass


# ── Server bootstrap ─────────────────────────────────────────────────────────

class _ThreadingHTTPServer(HTTPServer):
    """Tiny thread-per-request server so SSE streams don't block other requests."""
    daemon_threads = True

    def process_request(self, request, client_address):
        t = threading.Thread(
            target=self._handle_in_thread, args=(request, client_address), daemon=True,
        )
        t.start()

    def _handle_in_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as e:
            # Normal: client (SDK connection pool, browser, curl --max-time)
            # closed the socket before sending a request line, or mid-response.
            # Not an error — log at debug level only.
            logger.debug("Client closed connection early: %s", e)
        except Exception as e:
            logger.error("Request handler crashed: %s", e, exc_info=True)
        finally:
            try:
                self.shutdown_request(request)
            except Exception:
                pass


def run_server(host="0.0.0.0", port=8080, config_path="config.json"):
    global _config_path, _server_host, _server_port
    _config_path = config_path
    _server_host = host
    _server_port = port

    if not load_config():
        logger.warning("Initial configuration could not be loaded. Use /admin/reload to retry.")
    else:
        sync_ok, sync_info = sync_claude_settings()
        if sync_ok:
            logger.info("Claude settings synced on startup: %s", sync_info.get("path"))
        else:
            logger.warning("Claude settings sync skipped: %s", sync_info.get("error"))

    server = _ThreadingHTTPServer((host, port), ProxyHTTPRequestHandler)
    logger.info("=" * 60)
    logger.info("OpenAI → Claude Adapter Proxy")
    logger.info("Listening on http://%s:%d", host, port)
    logger.info("Configuration: %s", os.path.abspath(_config_path))
    logger.info("Endpoints:")
    logger.info("  GET  /v1/models                  - List available models")
    logger.info("  POST /v1/messages                - Anthropic Messages (streaming OK)")
    logger.info("  POST /v1/messages/count_tokens   - Token count (heuristic)")
    logger.info("  GET  /admin/health               - Health check")
    logger.info("  POST/GET /admin/reload           - Hot reload config")
    logger.info("=" * 60)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down server...")
        server.server_close()
        logger.info("Server stopped.")


def main():
    parser = argparse.ArgumentParser(description="OpenAI → Claude Adapter Proxy")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PROXY_PORT", 8080)),
                        help="Port to listen on (default: 8080, env: PROXY_PORT)")
    parser.add_argument("--config", type=str,
                        default=os.environ.get("PROXY_CONFIG", "config.json"),
                        help="Path to config file (default: config.json, env: PROXY_CONFIG)")
    parser.add_argument("--host", type=str, default="0.0.0.0",
                        help="Host to bind to (default: 0.0.0.0)")
    parser.add_argument("--log-dir", type=str,
                        default=os.environ.get("PROXY_LOG_DIR", str(_DEFAULT_LOG_DIR)),
                        help=f"Log directory (default: {_DEFAULT_LOG_DIR})")
    parser.add_argument("--log-max-bytes", type=int,
                        default=int(os.environ.get("PROXY_LOG_MAX_BYTES", _DEFAULT_LOG_MAX_BYTES)),
                        help="Max bytes per log file before rotation (default: ~100MB)")
    parser.add_argument("--log-backup-count", type=int,
                        default=int(os.environ.get("PROXY_LOG_BACKUP_COUNT", _DEFAULT_LOG_BACKUP_COUNT)),
                        help="Number of rotated log files to keep")
    parser.add_argument("--http-timeout", type=int,
                        default=int(os.environ.get("PROXY_HTTP_TIMEOUT", _DEFAULT_HTTP_TIMEOUT)),
                        help=f"Non-streaming socket timeout (default: {_DEFAULT_HTTP_TIMEOUT})")
    parser.add_argument("--stream-timeout", type=int,
                        default=int(os.environ.get("PROXY_STREAM_TIMEOUT", _DEFAULT_STREAM_TIMEOUT)),
                        help=f"Streaming idle timeout (default: {_DEFAULT_STREAM_TIMEOUT})")
    parser.add_argument("--stream-keepalive", type=int,
                        default=int(os.environ.get("PROXY_STREAM_KEEPALIVE", _DEFAULT_STREAM_KEEPALIVE)),
                        help=f"Seconds between SSE keep-alive pings (default: {_DEFAULT_STREAM_KEEPALIVE})")
    parser.add_argument("--max-retries", type=int,
                        default=int(os.environ.get("PROXY_MAX_RETRIES", _DEFAULT_MAX_RETRIES)),
                        help=f"Max retries for transient upstream errors (default: {_DEFAULT_MAX_RETRIES})")
    parser.add_argument("--upstream-idle-limit", type=int,
                        default=int(os.environ.get("PROXY_UPSTREAM_IDLE_LIMIT", _DEFAULT_UPSTREAM_IDLE_LIMIT)),
                        help="Proactively close stream after this many idle seconds (0=disabled)")
    parser.add_argument("--empty-stop-max-retry", type=int,
                        default=int(os.environ.get("PROXY_EMPTY_STOP_MAX_RETRY", _DEFAULT_EMPTY_STOP_MAX_RETRY)),
                        help=("Retry the upstream this many times when it returns an empty "
                              "finish=stop turn (no text/reasoning/tool, completion_tokens<=1). "
                              f"0 disables (default: {_DEFAULT_EMPTY_STOP_MAX_RETRY})"))
    args = parser.parse_args()

    setup_file_logging(
        log_dir=args.log_dir,
        max_bytes=args.log_max_bytes,
        backup_count=args.log_backup_count,
    )
    configure_timeouts(
        http_timeout=args.http_timeout,
        stream_timeout=args.stream_timeout,
        stream_keepalive=args.stream_keepalive,
        max_retries=args.max_retries,
        upstream_idle_limit=args.upstream_idle_limit,
        empty_stop_max_retry=args.empty_stop_max_retry,
    )
    logger.info(
        "Forward tunables: http=%ds stream-idle=%ds keepalive=%ds idle-limit=%ds retries=%d empty_stop_retry=%d",
        args.http_timeout, args.stream_timeout, args.stream_keepalive,
        args.upstream_idle_limit, args.max_retries, args.empty_stop_max_retry,
    )
    run_server(host=args.host, port=args.port, config_path=args.config)


if __name__ == "__main__":
    main()
