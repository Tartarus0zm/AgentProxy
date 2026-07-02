#!/usr/bin/env python3
"""
Claude Code API Proxy Service

A lightweight API proxy that sits between Claude Code CLI and Anthropic API.
Supports multi-model routing, hot-reload configuration, and SSE streaming.

Usage:
    python3 proxy.py [--port PORT] [--config CONFIG_FILE]
"""

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
from datetime import datetime, timezone
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# ── Logging ──────────────────────────────────────────────────────────────────

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Defaults for rotating file log
_DEFAULT_LOG_DIR = Path(__file__).resolve().parent / "log"
_DEFAULT_LOG_FILENAME = "proxy.log"
_DEFAULT_LOG_MAX_BYTES = 100 * 1024 * 1024  # 100 MB
_DEFAULT_LOG_BACKUP_COUNT = 6

# NOTE: We intentionally do NOT call logging.basicConfig() here.
# Logging policy:
#   - logger.* output  -> ONLY proxy.log (via RotatingFileHandler attached
#                         in setup_file_logging())
#   - print(...)       -> stdout -> captured by nohup into proxy.out
#   - print(..., file=sys.stderr) -> stderr -> captured into proxy.err
# A NullHandler is attached as a safety net so logging never falls back to
# the lastResort StreamHandler (which would write to stderr and pollute
# proxy.err).
logger = logging.getLogger("proxy")
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
    """
    Add a rotating file handler to the root logger so logs go to both
    the console AND a rolling file.

    - log_dir: directory for log files (default: <project_root>/log)
    - filename: log filename (default: proxy.log)
    - max_bytes: max size per file in bytes (default: 100 MB)
    - backup_count: number of rotated files to keep (default: 6)

    Rotated files: proxy.log, proxy.log.1, ..., proxy.log.6
    """
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
    # Avoid attaching duplicate file handlers if called twice
    for h in list(root_logger.handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler) and \
                getattr(h, "baseFilename", None) == handler.baseFilename:
            root_logger.removeHandler(h)
    # Drop any StreamHandler that would echo logs to stdout/stderr — we want
    # logger output to go ONLY to the rotating file. (NullHandlers are kept.)
    for h in list(root_logger.handlers):
        if type(h) is logging.StreamHandler:  # exact type, not subclasses (file handler is a subclass)
            root_logger.removeHandler(h)
    root_logger.addHandler(handler)

    # Quiet announcement to the file itself; do NOT print to stdout/stderr to
    # keep proxy.out/proxy.err untouched by logger output.
    logger.info(
        "File logging enabled: path=%s max_bytes=%d backup_count=%d",
        log_path, max_bytes, backup_count,
    )
    return log_path

# ── Configuration ────────────────────────────────────────────────────────────

_config = {}
_config_path = ""
_loaded = False

# Server runtime info (set by run_server) so /admin/reload knows the proxy URL
_server_host = "0.0.0.0"
_server_port = 8080

# Path to Claude Code CLI settings file
_CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


def load_config(config_path=None):
    """Load configuration from JSON file.

    Convention: config.json declares 3 or 4 entries (order matters):
      1st key -> ANTHROPIC_DEFAULT_OPUS_MODEL
      2nd key -> ANTHROPIC_DEFAULT_SONNET_MODEL
      3rd key -> ANTHROPIC_DEFAULT_HAIKU_MODEL
      4th key -> ANTHROPIC_DEFAULT_FABLE_MODEL  (claude-code v2.1.172+, optional)

    Missing slots fall back to the last available entry.
    """
    global _config, _config_path, _loaded

    if config_path:
        _config_path = config_path

    try:
        with open(_config_path, "r", encoding="utf-8") as f:
            _config = json.load(f)
        _loaded = True

        keys = list(_config.keys())
        logger.info("Configuration loaded: %d model(s) - %s", len(keys), ", ".join(keys))
        if len(keys) not in (3, 4):
            logger.warning(
                "config.json should contain 3 or 4 models (got %d). "
                "Slot mapping will fall back where missing.",
                len(keys),
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
    """Get current configuration."""
    return _config


def get_model_config(model_name):
    """Get configuration for a specific model."""
    return _config.get(model_name)


def is_loaded():
    """Check if configuration has been loaded."""
    return _loaded


def get_default_model():
    """Get the first available model name."""
    if _config:
        return next(iter(_config.keys()))
    return None


def get_default_model_config():
    """Get configuration for the default (first) model."""
    if _config:
        first_key = next(iter(_config.keys()))
        return first_key, _config[first_key]
    return None, None


# ── Claude Code settings sync ────────────────────────────────────────────────

_CLAUDE_MODEL_SLOTS = ("opus", "sonnet", "haiku", "fable")
_CLAUDE_SLOT_ENV_NAMES = {
    "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "fable": "ANTHROPIC_DEFAULT_FABLE_MODEL",
}


def _pick_model_for_slot(slot, model_ids):
    """Strict positional fallback: 1st->opus, 2nd->sonnet, 3rd->haiku, 4th->fable.

    If config has fewer entries than the slot index requires, missing slots
    fall back to the last available entry.
    """
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
    """Build the local proxy URL that Claude Code should talk to."""
    host = _server_host
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return f"http://{host}:{_server_port}"


def sync_claude_settings():
    """
    Update ~/.claude/settings.json so the Claude Code CLI talks to this proxy
    and the /model menu slots map onto the models declared in config.json.

    Returns a tuple: (success: bool, info: dict)
    """
    model_ids = list(_config.keys())
    if not model_ids:
        return False, {"error": "No models in proxy configuration; nothing to sync"}

    mapping, explicit_mapping = _build_slot_mapping(_config)
    opus_id = mapping["opus"]
    sonnet_id = mapping["sonnet"]
    haiku_id = mapping["haiku"]
    fable_id = mapping["fable"]

    base_url = _proxy_base_url()
    # Default model: always the FIRST entry in config.json
    default_model = model_ids[0]

    env_block = {
        "ANTHROPIC_AUTH_TOKEN": "proxy-local-token",
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


# ── Model capabilities template ─────────────────────────────────────────────

_MODEL_CAPABILITIES = {
    "batch": {"supported": True},
    "citations": {"supported": True},
    "code_execution": {"supported": True},
    "context_management": {
        "supported": True,
        "clear_thinking_20251015": {"supported": True},
        "clear_tool_uses_20250919": {"supported": True},
        "compact_20260112": {"supported": True},
    },
    "effort": {
        "supported": True,
        "low": {"supported": True},
        "medium": {"supported": True},
        "high": {"supported": True},
        "max": {"supported": True},
        "xhigh": {"supported": True},
    },
    "image_input": {"supported": True},
    "pdf_input": {"supported": True},
    "structured_outputs": {"supported": True},
    "thinking": {
        "supported": True,
        "types": {
            "adaptive": {"supported": True},
            "enabled": {"supported": True},
        },
    },
}

# Default display name mapping
_DISPLAY_NAMES = {
    "claude-opus-4-6-20260101": "Claude Opus 4.6",
    "claude-opus-4-8-20260101": "Claude Opus 4.8",
    "claude-sonnet-4-6-20260101": "Claude Sonnet 4.6",
    "claude-haiku-4-5-20260101": "Claude Haiku 4.5",
    "claude-fable-5-20260101": "Claude Fable 5",
    "claude-deepseek-v4-flash-20260101": "DeepSeek V4 Flash",
}

# ── Helper functions ─────────────────────────────────────────────────────────

def get_model_display_name(model_id):
    """Get human-readable display name for a model."""
    return _DISPLAY_NAMES.get(model_id, model_id)


def build_models_response():
    """Build the /v1/models response from config keys."""
    config = get_config()
    data = []

    for model_id in config.keys():
        data.append({
            "type": "model",
            "id": model_id,
            "display_name": get_model_display_name(model_id),
            "created_at": "2026-01-01T00:00:00Z",
        })

    model_ids = [m["id"] for m in data]
    return {
        "data": data,
        "first_id": model_ids[0] if model_ids else None,
        "has_more": False,
        "last_id": model_ids[-1] if model_ids else None,
    }


def extract_model_from_body(body):
    """Extract model name from request body JSON."""
    try:
        data = json.loads(body)
        return data.get("model")
    except (json.JSONDecodeError, TypeError):
        return None


def extract_model_from_batch_body(body):
    """Extract model name from batch request body (first request's params.model)."""
    try:
        data = json.loads(body)
        requests_list = data.get("requests", [])
        if requests_list:
            params = requests_list[0].get("params", {})
            return params.get("model")
    except (json.JSONDecodeError, TypeError):
        return None
    return None


# ── HTTP forwarding timeouts (configurable) ──────────────────────────────────
# Inspired by Anthropic's official @anthropic-ai/sdk + undici defaults:
#   - non-streaming total timeout : 600s  (SDK default)
#   - streaming chunk-idle timeout: 300s  (undici bodyTimeout default)
#   - SSE keep-alive ping interval: 20s   (server normally pings every ~25s;
#     we send slightly more often as belt-and-braces if upstream stays silent)
#   - max retries (idempotent / pre-stream): 2 with exponential backoff
_DEFAULT_HTTP_TIMEOUT = 600              # seconds for non-streaming
_DEFAULT_STREAM_TIMEOUT = 600            # seconds — upper bound on socket idle
                                         # for streaming. Longer than the SDK
                                         # default (which relies on undici's
                                         # 300s bodyTimeout) on purpose, so
                                         # that long upstream "thinking" gaps
                                         # don't cause us to give up first.
_DEFAULT_STREAM_KEEPALIVE = 0            # seconds between proxy-injected SSE pings.
                                         # Default is 0 because this proxy must be
                                         # transparent for Anthropic SSE streams:
                                         # Claude Code and the upstream speak the
                                         # same protocol, so the proxy must not
                                         # inject extra events/comments unless the
                                         # operator explicitly opts in.
_DEFAULT_UPSTREAM_IDLE_LIMIT = 0         # 0 = disabled. Only set >0 if you want
                                         # the proxy to proactively close streams
                                         # when upstream truly stops sending data
                                         # for that many seconds. Independent of
                                         # _stream_timeout, which is a hard upper
                                         # bound (default 300s).
_DEFAULT_MAX_RETRIES = 2                 # retries for connect / pre-stream errors
_DEFAULT_RETRY_BASE_DELAY = 0.5          # seconds (exponential backoff base)

_http_timeout = _DEFAULT_HTTP_TIMEOUT
_stream_timeout = _DEFAULT_STREAM_TIMEOUT
_stream_keepalive = _DEFAULT_STREAM_KEEPALIVE
_upstream_idle_limit = _DEFAULT_UPSTREAM_IDLE_LIMIT
_max_retries = _DEFAULT_MAX_RETRIES

# Optional diagnostics for tool-use corruption. Enabled by default because it
# logs only compact metadata (tool names / input types / delta byte counts), not
# full prompts or tool arguments. Set CLAUDE_PROXY_TOOL_DIAG=0 to disable.
_TOOL_DIAG_ENABLED = os.environ.get("CLAUDE_PROXY_TOOL_DIAG", "1").lower() not in {
    "0", "false", "no", "off"
}

# Optional auto-repair for known model-side tool-input schema mistakes.
#
# HISTORY / WARNING:
# This rewriter was introduced (2026-06-26) to patch `AskUserQuestion`
# tool_use inputs that were missing the `question` field. It is DISABLED BY
# DEFAULT (2026-06-29) because in production it caused worse problems than it
# fixed: when other content blocks (`thinking`, non-AskUserQuestion `tool_use`)
# interleaved with the patched block, the SSE-state-machine occasionally
# dropped legitimate content blocks, producing assistant messages with
# `stop_reason="tool_use"` but no tool_use content — which permanently breaks
# the conversation. The Anthropic protocol is byte-transparent; rewriting
# upstream responses is the wrong layer to repair model misbehaviour.
#
# Set CLAUDE_PROXY_TOOL_REPAIR=1 to re-enable for investigation only.
_TOOL_INPUT_REPAIR_ENABLED = os.environ.get(
    "CLAUDE_PROXY_TOOL_REPAIR", "0"
).lower() not in {"0", "false", "no", "off"}

# Optional raw request/response capture for proxy-vs-direct comparison.
# When CLAUDE_PROXY_CAPTURE_DIR is set, each streaming request dumps:
#   <dir>/<ts>-<reqid>.req.json       — the raw request body bytes
#   <dir>/<ts>-<reqid>.req-headers.json — forwarded headers (auth masked)
#   <dir>/<ts>-<reqid>.upstream.sse   — raw upstream SSE bytes (pre-rewriter)
#   <dir>/<ts>-<reqid>.target.txt     — target URL
# This is a *diagnostic only* feature: capture happens at the byte boundary
# the proxy actually forwards/receives. To reproduce against the upstream
# directly, use bin/replay_capture.sh <capture-dir>/<prefix>.
_CAPTURE_DIR = os.environ.get("CLAUDE_PROXY_CAPTURE_DIR", "").strip() or None
if _CAPTURE_DIR:
    try:
        os.makedirs(_CAPTURE_DIR, exist_ok=True)
    except Exception:
        _CAPTURE_DIR = None

_PATCHABLE_TOOLS = {"AskUserQuestion"}


# ── XML-as-text tool-call leak guard (B+C from incident 2026-06-29) ──────────
#
# Observed pattern: claude-opus-4-8 occasionally emits its tool call as a
# plain `text` content block containing literal XML, e.g.
#     <invoke name="Bash">
#       <parameter name="command">...</parameter>
#     </invoke>
# instead of a proper `tool_use` content block. Because the proxy is byte-
# transparent, Claude Code receives the text as user-visible output and the
# tool never runs. Once a single XML-leak text block lands in the session
# history, the model is in-context-learning-primed to repeat the same XML
# pattern on subsequent turns — a self-poisoning feedback loop.
#
# Two-part mitigation:
#   C) ALWAYS count leaks per request and log a structured warning. This
#      gives us visibility without changing bytes.
#   B) When CLAUDE_PROXY_XML_LEAK_GUARD is enabled (default ON), abort the
#      stream the moment we detect the leak by injecting an SSE `error` +
#      `message_stop` frame using error type `overloaded_error`. Claude Code
#      treats that as a transient upstream issue and retries the same
#      request, which empirically succeeds ~98% of the time. The injection
#      reuses the exact same idle-timeout pattern already proven safe in
#      `_handle_streaming_forward`.
#
# Safety:
#   * Guard only fires for `text_delta` events. Thinking/tool_use blocks
#     are untouched.
#   * The detection window is the *accumulated* text of a single text
#     content block; we never look across blocks. Once a block stops, its
#     accumulator is cleared.
#   * If `<invoke name=` is detected, the offending line is NOT forwarded,
#     and no further upstream bytes are forwarded either.
#   * The synthetic error is only emitted if the proxy hasn't already
#     forwarded a `message_stop` (it can't have, because detection happens
#     mid-stream before message_stop).
#   * Disable with CLAUDE_PROXY_XML_LEAK_GUARD=0 to fall back to pure
#     observability (C only).
_XML_LEAK_GUARD_ENABLED = os.environ.get(
    "CLAUDE_PROXY_XML_LEAK_GUARD", "1"
).lower() not in {"0", "false", "no", "off"}


class XmlLeakGuard:
    """Detect XML-as-text tool-call leak in an Anthropic SSE stream.

    Usage:
        guard = XmlLeakGuard(model)
        for line in upstream:
            verdict = guard.inspect(line)
            if verdict.drop_line:
                # Do not forward this line.
                if verdict.should_abort:
                    write_synthetic_overloaded_and_stop()
                    break
            else:
                forward(line)

    The class is stateful per stream: instantiate one per request.
    """

    # Tokens that unambiguously indicate the model has switched into XML
    # tool-call format inside a `text` content block. We match on the
    # accumulated text of the current block, so partial sequences like
    # "<invoke" arriving across multiple deltas still trigger.
    _TRIGGER = "<invoke name="
    # Keep the per-block accumulator bounded: if the model produces a huge
    # benign text block, we don't want to balloon memory.
    _MAX_ACCUM = 4096

    def __init__(self, model):
        self.model = model
        # idx -> accumulated text for blocks of type=text. Other block
        # types are not tracked.
        self._text_acc = {}
        # Once tripped, every subsequent inspect() drops the line so we
        # don't leak any more upstream bytes downstream.
        self._tripped = False
        self._trip_info = None  # dict for logging

    # ── public API ──────────────────────────────────────────────────
    def inspect(self, line):
        """Inspect one upstream SSE line. Return an _XmlGuardVerdict."""
        if self._tripped:
            # Already tripped — silently drop remaining lines. The caller
            # will have written the synthetic stop already.
            return _XmlGuardVerdict(drop_line=True, should_abort=False)

        # Only parse `data:` payloads. event:/blank lines are harmless.
        if not line or not line.startswith(b"data:"):
            return _XmlGuardVerdict(drop_line=False, should_abort=False)
        try:
            payload = line[len(b"data:"):].strip()
            if not payload:
                return _XmlGuardVerdict(drop_line=False, should_abort=False)
            data = json.loads(payload)
        except Exception:
            return _XmlGuardVerdict(drop_line=False, should_abort=False)

        typ = data.get("type")
        if typ == "content_block_start":
            idx = data.get("index")
            block = data.get("content_block") or {}
            if block.get("type") == "text":
                self._text_acc[idx] = ""
            return _XmlGuardVerdict(drop_line=False, should_abort=False)

        if typ == "content_block_stop":
            idx = data.get("index")
            self._text_acc.pop(idx, None)
            return _XmlGuardVerdict(drop_line=False, should_abort=False)

        if typ == "content_block_delta":
            idx = data.get("index")
            if idx not in self._text_acc:
                return _XmlGuardVerdict(drop_line=False, should_abort=False)
            delta = data.get("delta") or {}
            if delta.get("type") != "text_delta":
                return _XmlGuardVerdict(drop_line=False, should_abort=False)
            chunk = delta.get("text") or ""
            if not chunk:
                return _XmlGuardVerdict(drop_line=False, should_abort=False)
            acc = self._text_acc[idx] + chunk
            # Bound memory.
            if len(acc) > self._MAX_ACCUM:
                acc = acc[-self._MAX_ACCUM:]
            self._text_acc[idx] = acc
            if self._TRIGGER in acc:
                self._tripped = True
                # Snippet for log (avoid logging huge prompts).
                hit = acc.find(self._TRIGGER)
                snippet = acc[max(0, hit - 40): hit + 200]
                self._trip_info = {
                    "block_index": idx,
                    "accum_len": len(acc),
                    "snippet": snippet,
                }
                return _XmlGuardVerdict(drop_line=True, should_abort=True)

        return _XmlGuardVerdict(drop_line=False, should_abort=False)

    @property
    def tripped(self):
        return self._tripped

    @property
    def trip_info(self):
        return self._trip_info


class _XmlGuardVerdict:
    """Return value from XmlLeakGuard.inspect()."""
    __slots__ = ("drop_line", "should_abort")

    def __init__(self, drop_line, should_abort):
        self.drop_line = drop_line
        self.should_abort = should_abort


def _repair_tool_input(name, inp):
    """Apply known schema-fix rules to a tool_use input dict in-place.

    Returns the (possibly mutated) input dict, plus a list of human-readable
    patch tags for logging. The input is mutated in place when possible so
    callers can also keep the original reference if they want.
    """
    patches = []
    if not isinstance(inp, dict):
        return inp, patches

    if name == "AskUserQuestion":
        questions = inp.get("questions")
        if isinstance(questions, list):
            for i, q in enumerate(questions):
                if not isinstance(q, dict):
                    continue
                if not q.get("question"):
                    header = q.get("header") or ""
                    fallback = header.strip() if isinstance(header, str) else ""
                    if not fallback:
                        fallback = "请选择"
                    # Ensure it reads like a question. Strip trailing colons,
                    # then append a question mark if absent.
                    fallback = fallback.rstrip(":：?？")
                    if not fallback.endswith(("?", "？")):
                        fallback = fallback + "？"
                    q["question"] = fallback
                    patches.append(f"questions[{i}].question<-header")
    return inp, patches


def _repair_non_stream_body(resp_body):
    """Apply _repair_tool_input to a non-streaming Anthropic /v1/messages
    response. Best-effort: any decode/parse/serialize failure returns the
    original bytes unchanged."""
    try:
        text = resp_body.decode("utf-8")
    except Exception:
        return resp_body
    try:
        obj = json.loads(text)
    except Exception:
        return resp_body
    if not isinstance(obj, dict):
        return resp_body
    content = obj.get("content")
    if not isinstance(content, list):
        return resp_body
    total_patches = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        name = block.get("name")
        if name not in _PATCHABLE_TOOLS:
            continue
        inp = block.get("input")
        repaired, patches = _repair_tool_input(name, inp)
        if patches:
            block["input"] = repaired
            total_patches.append((name, block.get("id"), patches))
    if not total_patches:
        return resp_body
    try:
        new_bytes = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    except Exception:
        return resp_body
    for name, tool_id, patches in total_patches:
        logger.info(
            "TOOL_REPAIR patched (non-stream) name=%s id=%s patches=%s",
            name, tool_id, patches,
        )
    return new_bytes


class StreamToolDiagnostics:
    """Parse already-forwarded Anthropic SSE lines and log tool-use integrity.

    The proxy is supposed to be byte-for-byte transparent for /v1/messages
    streams. If Claude Code later reports `Invalid tool parameters`, this
    diagnostic tells us whether the upstream stream itself contained an empty or
    malformed tool input, or whether input deltas were seen on the wire.
    """

    def __init__(self, model):
        self.model = model
        self.event = None
        self.data_lines = []
        self.blocks = {}

    def feed(self, line):
        if not _TOOL_DIAG_ENABLED:
            return
        try:
            text = line.decode("utf-8", errors="replace")
        except Exception:
            return

        stripped = text.rstrip("\r\n")
        if not stripped:
            self._finish_event()
            return
        if stripped.startswith(":"):
            return
        if stripped.startswith("event:"):
            self.event = stripped[len("event:"):].strip()
            return
        if stripped.startswith("data:"):
            self.data_lines.append(stripped[len("data:"):].lstrip())

    def _finish_event(self):
        if not self.data_lines:
            self.event = None
            return
        raw_data = "\n".join(self.data_lines)
        event = self.event
        self.event = None
        self.data_lines = []
        try:
            data = json.loads(raw_data)
        except Exception:
            return

        typ = data.get("type") or event
        if typ == "content_block_start":
            idx = data.get("index")
            block = data.get("content_block") or {}
            if block.get("type") == "tool_use":
                initial_input = block.get("input")
                rec = {
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "initial_input_type": type(initial_input).__name__,
                    "initial_input_empty": initial_input == {},
                    "delta_bytes": 0,
                    "delta_count": 0,
                    "partial_json": "",
                }
                self.blocks[idx] = rec
                if initial_input in ({}, None) or not block.get("name"):
                    logger.warning(
                        "TOOL_DIAG start suspicious model=%s index=%s id=%s name=%r "
                        "initial_input_type=%s initial_input_empty=%s",
                        self.model, idx, rec["id"], rec["name"],
                        rec["initial_input_type"], rec["initial_input_empty"],
                    )
        elif typ == "content_block_delta":
            idx = data.get("index")
            delta = data.get("delta") or {}
            if delta.get("type") == "input_json_delta":
                rec = self.blocks.setdefault(idx, {})
                frag = delta.get("partial_json") or ""
                rec["delta_bytes"] = rec.get("delta_bytes", 0) + len(frag.encode("utf-8"))
                rec["delta_count"] = rec.get("delta_count", 0) + 1
                # Keep only tool argument deltas. This is bounded by a single
                # tool input and used only for schema-level diagnostics.
                rec["partial_json"] = rec.get("partial_json", "") + frag
        elif typ == "content_block_stop":
            idx = data.get("index")
            rec = self.blocks.get(idx)
            if rec and rec.get("name"):
                self._log_tool_stop(idx, rec)

    def _summarize_reconstructed_input(self, rec):
        partial = rec.get("partial_json") or ""
        if not partial:
            return {"json_ok": None, "keys": [], "error": "no_partial_json"}
        try:
            reconstructed = json.loads(partial)
        except json.JSONDecodeError as exc:
            pos = getattr(exc, "pos", None)
            around = partial[max(0, pos - 120):pos + 120] if isinstance(pos, int) else ""
            return {
                "json_ok": False,
                "keys": [],
                "error": type(exc).__name__,
                "msg": exc.msg,
                "pos": pos,
                "lineno": exc.lineno,
                "colno": exc.colno,
                "len": len(partial),
                "preview": partial[:160],
                "around_error": around,
                "tail": partial[-240:],
            }
        except Exception as exc:
            return {
                "json_ok": False,
                "keys": [],
                "error": type(exc).__name__,
                "len": len(partial),
                "preview": partial[:160],
                "tail": partial[-240:],
            }
        if not isinstance(reconstructed, dict):
            return {
                "json_ok": True,
                "type": type(reconstructed).__name__,
                "keys": [],
            }
        summary = {
            "json_ok": True,
            "type": "dict",
            "keys": sorted(reconstructed.keys()),
        }
        if rec.get("name") == "AskUserQuestion":
            questions = reconstructed.get("questions")
            summary.update({
                "questions_type": type(questions).__name__,
                "questions_len": len(questions) if isinstance(questions, list) else None,
                "question_keys_0": (
                    sorted(questions[0].keys())
                    if isinstance(questions, list)
                    and questions
                    and isinstance(questions[0], dict)
                    else None
                ),
            })
        elif rec.get("name") == "Bash":
            summary.update({
                "has_command": isinstance(reconstructed.get("command"), str),
                "has_description": isinstance(reconstructed.get("description"), str),
            })
        return summary

    def _log_tool_stop(self, idx, rec):
        input_summary = self._summarize_reconstructed_input(rec)
        if rec.get("initial_input_empty") and rec.get("delta_bytes", 0) == 0:
            logger.warning(
                "TOOL_DIAG stop EMPTY_INPUT model=%s index=%s id=%s name=%s "
                "delta_count=%d delta_bytes=%d reconstructed=%s",
                self.model, idx, rec.get("id"), rec.get("name"),
                rec.get("delta_count", 0), rec.get("delta_bytes", 0), input_summary,
            )
        else:
            log = logger.warning if not input_summary.get("json_ok") else logger.info
            log(
                "TOOL_DIAG stop model=%s index=%s id=%s name=%s "
                "initial_empty=%s delta_count=%d delta_bytes=%d reconstructed=%s",
                self.model, idx, rec.get("id"), rec.get("name"),
                rec.get("initial_input_empty"), rec.get("delta_count", 0),
                rec.get("delta_bytes", 0), input_summary,
            )


# NOTE: Do not normalize or rewrite upstream Anthropic responses here.
# Claude Code and the upstream Wanqing Claude API speak the same protocol, so
# response-body transparency is the root invariant of this proxy. Keep any
# response inspection diagnostic-only.
#
# Exception: when CLAUDE_PROXY_TOOL_REPAIR is on, the rewriter below patches a
# small, allow-listed set of known-bad model outputs (see _PATCHABLE_TOOLS and
# _repair_tool_input). All other bytes are forwarded byte-for-byte.


class StreamToolInputRewriter:
    """Stream-time repair for known broken tool_use inputs.

    Strategy per SSE event:
      * `message_start`, `ping`, `text_delta`, `message_delta`, `message_stop`,
        comments, and any non-tool-use content blocks → forward as-is.
      * `content_block_start` with `tool_use` whose name is in
        `_PATCHABLE_TOOLS` → forward as-is, mark this index as "interested",
        and start buffering its `input_json_delta` lines.
      * `content_block_delta` on a buffered index → BUFFER, do not forward.
      * `content_block_stop` on a buffered index → try to parse the buffered
        partial JSON, repair it, then emit a SINGLE synthetic
        `input_json_delta` carrying the full repaired JSON, immediately
        followed by the original `content_block_stop`. If parse fails or no
        patches needed, flush the buffered deltas verbatim.
      * For non-interested indexes everything passes through untouched.

    Output is a list of `bytes` lines (already terminated with `\\n`).
    """

    def __init__(self, model):
        self.model = model
        self.event = None         # current SSE `event:` line value
        self.data_lines = []      # current SSE `data:` payload accumulator (raw)
        self.raw_lines = []       # raw bytes for the current event, in case
                                  # we need to forward it verbatim
        # Per-block state: blocks[index] = {"name", "buffered": [bytes],
        # "partial_json": str, "active": bool}
        self.blocks = {}
        self._last_consumed = False

    # ── public API ───────────────────────────────────────────────────────
    def feed(self, line):
        """Consume one upstream SSE line. Returns a list of bytes lines to
        emit to Claude Code (may be empty, may be more than one)."""
        if not _TOOL_INPUT_REPAIR_ENABLED:
            return [line]
        try:
            text = line.decode("utf-8", errors="replace")
        except Exception:
            return [line]

        stripped = text.rstrip("\r\n")
        # SSE comment / keep-alive — pass through, not part of any event.
        if stripped.startswith(":"):
            return [line]

        # Blank line terminates an event.
        if not stripped:
            out = self._finish_event()
            # Always forward the blank terminator unless the event was
            # entirely consumed (buffered). If buffered, the blank line
            # still needs to be suppressed so we don't break SSE framing
            # on the client side for an event that was never emitted.
            if self._last_consumed:
                return out
            return out + [line]

        if stripped.startswith("event:"):
            self.event = stripped[len("event:"):].strip()
            self.raw_lines.append(line)
            return []
        if stripped.startswith("data:"):
            self.data_lines.append(stripped[len("data:"):].lstrip())
            self.raw_lines.append(line)
            return []

        # Unknown line — forward verbatim and reset framing state.
        self.event = None
        self.data_lines = []
        self.raw_lines = []
        return [line]

    # ── internals ────────────────────────────────────────────────────────
    def _finish_event(self):
        self._last_consumed = False
        raw_lines = self.raw_lines
        data_lines = self.data_lines
        event = self.event
        self.event = None
        self.data_lines = []
        self.raw_lines = []

        if not data_lines:
            return []
        raw_data = "\n".join(data_lines)
        try:
            data = json.loads(raw_data)
        except Exception:
            return raw_lines  # malformed JSON — forward as-is

        typ = data.get("type") or event

        if typ == "content_block_start":
            idx = data.get("index")
            block = data.get("content_block") or {}
            if (
                block.get("type") == "tool_use"
                and block.get("name") in _PATCHABLE_TOOLS
            ):
                self.blocks[idx] = {
                    "name": block.get("name"),
                    "buffered": [],
                    "partial_json": "",
                    "active": True,
                }
            return raw_lines

        if typ == "content_block_delta":
            idx = data.get("index")
            rec = self.blocks.get(idx)
            if rec and rec.get("active"):
                delta = data.get("delta") or {}
                if delta.get("type") == "input_json_delta":
                    rec["buffered"].extend(raw_lines)
                    # Preserve the blank-line terminator for this event so a
                    # later verbatim flush reproduces correct SSE framing.
                    rec["buffered"].append(b"\n")
                    rec["partial_json"] += delta.get("partial_json") or ""
                    self._last_consumed = True
                    return []
                # Non-input_json_delta on a tool_use block (unexpected for
                # tool_use, but possible) — flush buffer first, then forward.
                flushed = rec["buffered"]
                rec["buffered"] = []
                return flushed + raw_lines
            return raw_lines

        if typ == "content_block_stop":
            idx = data.get("index")
            rec = self.blocks.pop(idx, None)
            if rec and rec.get("active"):
                return self._finalize_block(idx, rec, raw_lines)
            return raw_lines

        return raw_lines

    def _finalize_block(self, idx, rec, stop_lines):
        partial = rec.get("partial_json") or ""
        buffered = rec.get("buffered") or []
        name = rec.get("name")

        if not partial:
            # No deltas at all — nothing to repair, just forward stop.
            return stop_lines

        try:
            obj = json.loads(partial)
        except Exception as exc:
            logger.warning(
                "TOOL_REPAIR parse_fail model=%s index=%s name=%s err=%s len=%d; "
                "forwarding original deltas",
                self.model, idx, name, type(exc).__name__, len(partial),
            )
            return buffered + stop_lines

        repaired, patches = _repair_tool_input(name, obj)
        if not patches:
            # Schema looks fine — pass through unchanged.
            return buffered + stop_lines

        try:
            new_json = json.dumps(repaired, ensure_ascii=False)
        except Exception as exc:
            logger.warning(
                "TOOL_REPAIR serialize_fail model=%s index=%s name=%s err=%s; "
                "forwarding original deltas",
                self.model, idx, name, type(exc).__name__,
            )
            return buffered + stop_lines

        synthetic = {
            "type": "content_block_delta",
            "index": idx,
            "delta": {
                "type": "input_json_delta",
                "partial_json": new_json,
            },
        }
        synthetic_lines = (
            b"event: content_block_delta\n"
            + b"data: "
            + json.dumps(synthetic, ensure_ascii=False).encode("utf-8")
            + b"\n"
        )
        logger.info(
            "TOOL_REPAIR patched model=%s index=%s name=%s patches=%s "
            "orig_len=%d new_len=%d",
            self.model, idx, name, patches, len(partial), len(new_json),
        )
        # Emit: synthetic delta line, blank terminator, then original stop.
        return [synthetic_lines, b"\n"] + stop_lines


def summarize_request_tools(body):
    """Return compact request tool-schema diagnostics without logging prompts."""
    if not _TOOL_DIAG_ENABLED:
        return None
    try:
        req = json.loads(body)
    except Exception:
        return None
    tools = req.get("tools") or []
    if not isinstance(tools, list):
        return {"tools_type": type(tools).__name__}
    names = []
    ask_schema = None
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if name:
            names.append(name)
        if name == "AskUserQuestion":
            schema = tool.get("input_schema") or {}
            props = schema.get("properties") if isinstance(schema, dict) else None
            questions = props.get("questions") if isinstance(props, dict) else None
            ask_schema = {
                "required": schema.get("required") if isinstance(schema, dict) else None,
                "questions_type": questions.get("type") if isinstance(questions, dict) else None,
                "questions_items_type": (
                    questions.get("items", {}).get("type")
                    if isinstance(questions, dict) and isinstance(questions.get("items"), dict)
                    else None
                ),
            }
    return {
        "tool_count": len(tools),
        "has_AskUserQuestion": "AskUserQuestion" in names,
        "has_TaskCreate": "TaskCreate" in names,
        "has_TaskList": "TaskList" in names,
        "tool_names": names,
        "AskUserQuestion_schema": ask_schema,
    }

_RETRIABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


def configure_timeouts(
    http_timeout=None, stream_timeout=None,
    stream_keepalive=None, max_retries=None,
    upstream_idle_limit=None,
):
    """Apply runtime tunables. None = leave unchanged."""
    global _http_timeout, _stream_timeout, _stream_keepalive, _max_retries
    global _upstream_idle_limit
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


def _compute_backoff(attempt, retry_after_header=None):
    """
    Exponential backoff with full jitter, mirroring Anthropic SDK behaviour.
    attempt: 0-based retry attempt index.
    retry_after_header: value of Retry-After header (seconds or HTTP-date),
      preferred when present.
    """
    # Honor server-provided Retry-After (seconds form first)
    if retry_after_header:
        try:
            return max(0.0, float(retry_after_header))
        except (TypeError, ValueError):
            pass
    base = _DEFAULT_RETRY_BASE_DELAY * (2 ** attempt)
    cap = 8.0
    return random.uniform(0.0, min(cap, base))


def forward_request(method, target_base, target_path, headers, body, stream=False):
    """
    Forward an HTTP request to the target host, with SDK-style retries for
    transient failures (network errors / 408 / 409 / 429 / 5xx).

    For stream=True, retries only apply BEFORE the response headers arrive.
    Once the upstream starts streaming, we never auto-retry (would corrupt
    a partially-delivered, billed conversation).

    Returns:
      stream=False -> (status_code, response_headers, response_body_bytes)
      stream=True  -> (status_code, response_headers, response_obj, conn)
    """
    import http.client

    # Parse base URL properly so we don't pass a path into the host argument
    if "://" not in target_base:
        target_base = "https://" + target_base
    parsed = urlparse(target_base)
    scheme = parsed.scheme or "https"
    target_host_clean = parsed.hostname or ""
    port = parsed.port or (443 if scheme == "https" else 80)
    base_path = parsed.path.rstrip("/")

    # Combine base path with the requested target path (preserve query)
    if target_path.startswith("/"):
        full_path = base_path + target_path
    else:
        full_path = base_path + "/" + target_path
    if not full_path.startswith("/"):
        full_path = "/" + full_path

    # Build forwarded headers
    fwd_headers = {}
    # Strip hop-by-hop headers AND `accept-encoding`. Why drop accept-encoding?
    # SSE + gzip is a classic foot-gun: many gateways enable gzip when the
    # client requests it, but their gzip implementation does not flush after
    # every event (i.e. no Z_SYNC_FLUSH), so the stream buffers in the
    # compression layer and stalls until enough bytes accumulate for a
    # deflate block. The result is exactly what we observed:
    #   * curl (no Accept-Encoding) -> works perfectly
    #   * Claude CLI (Accept-Encoding: gzip,deflate,br) -> stalls mid-stream
    # Forcing identity encoding on the upstream side eliminates this and
    # also lets us pass the bytes through to the client untouched.
    skip_headers = {
        "host",
        "x-api-key",
        "content-length",
        "connection",
        "transfer-encoding",
        "accept-encoding",
    }
    for key, value in headers.items():
        if key.lower() not in skip_headers:
            fwd_headers[key] = value
    # Explicitly tell upstream we want no compression. Some gateways still
    # apply a default if the header is missing, so set it explicitly.
    fwd_headers["Accept-Encoding"] = "identity"

    # Set content-length if body exists
    if body:
        fwd_headers["Content-Length"] = str(len(body))

    use_ssl = scheme == "https"
    sock_timeout = _stream_timeout if stream else _http_timeout

    def _tune_long_idle_socket(sock):
        """
        Enable TCP keepalive with aggressive parameters so the connection
        survives long pauses (model "thinking" time, slow first-byte, etc.)
        without being silently reaped by intermediate middleboxes
        (NAT / firewall / corporate proxy / load balancer). Without this,
        many environments will RST the TCP connection after ~120s of
        no traffic, which we observed both for streaming AND non-streaming
        requests.

        Notes on platform constants (this is where we previously had a bug):
          * macOS Python 3.9 does NOT expose `socket.TCP_KEEPALIVE` even
            though the kernel supports it. The kernel-level optname is
            0x10 (16). If we just rely on `hasattr(socket, "TCP_KEEPALIVE")`
            we end up enabling SO_KEEPALIVE without ever setting the idle
            time, so probes don't start until macOS's default 7200s.
            Result: middlebox kills the connection at 120s, we never sent
            a single keepalive packet.
          * On Linux the optname is `TCP_KEEPIDLE` = 4.
            Python normally exposes this constant.
          * We fall back to hardcoded numeric optnames per-platform.

        Verifies the settings landed by reading them back via getsockopt.
        """
        if sock is None:
            return

        # Resolve the "idle" optname per platform with a safe fallback.
        idle_optname = None
        if hasattr(socket, "TCP_KEEPIDLE"):
            idle_optname = socket.TCP_KEEPIDLE              # Linux
        elif hasattr(socket, "TCP_KEEPALIVE"):
            idle_optname = socket.TCP_KEEPALIVE             # macOS (when exposed)
        elif sys.platform == "darwin":
            idle_optname = 0x10                             # macOS hardcoded
        elif sys.platform.startswith("linux"):
            idle_optname = 4                                # Linux hardcoded

        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if idle_optname is not None:
                sock.setsockopt(socket.IPPROTO_TCP, idle_optname, 30)
            if hasattr(socket, "TCP_KEEPINTVL"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 15)
            if hasattr(socket, "TCP_KEEPCNT"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)

            # Read back to verify.
            try:
                ka = sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
                idle_val = (
                    sock.getsockopt(socket.IPPROTO_TCP, idle_optname)
                    if idle_optname is not None else None
                )
                intvl = (
                    sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL)
                    if hasattr(socket, "TCP_KEEPINTVL") else None
                )
                cnt = (
                    sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT)
                    if hasattr(socket, "TCP_KEEPCNT") else None
                )
                logger.info(
                    "TCP keepalive on fd=%s: SO_KEEPALIVE=%s idle=%ss intvl=%ss cnt=%s "
                    "(idle_optname=%s)",
                    sock.fileno() if hasattr(sock, "fileno") else "?",
                    ka, idle_val, intvl, cnt, idle_optname,
                )
            except Exception as ge:
                logger.warning("Could not read back keepalive opts: %s", ge)
        except (OSError, AttributeError) as ke:
            logger.warning("setsockopt(keepalive) failed: %s", ke)

    last_error = None
    for attempt in range(_max_retries + 1):
        conn = None
        try:
            if use_ssl:
                conn = http.client.HTTPSConnection(target_host_clean, port, timeout=sock_timeout)
            else:
                conn = http.client.HTTPConnection(target_host_clean, port, timeout=sock_timeout)

            conn.request(method, full_path, body=body, headers=fwd_headers)

            # CRITICAL: enable TCP keepalive AS SOON AS the connection exists,
            # for BOTH streaming and non-streaming requests. The non-streaming
            # path also sits idle waiting for the upstream to finish thinking,
            # and middleboxes will silently RST the connection at ~120s of TCP
            # idle without keepalive packets flowing.
            try:
                if hasattr(conn, "sock") and conn.sock is not None:
                    _tune_long_idle_socket(conn.sock)
                    # Apply application-level read timeout for non-streaming.
                    # (Streaming will reset this below to _stream_timeout.)
                    conn.sock.settimeout(sock_timeout)
            except Exception:
                pass

            response = conn.getresponse()

            # Re-tune for streaming (idempotent), in case http.client wrapped
            # the socket differently after getresponse().
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

            # Retry on transient HTTP statuses ONLY when:
            #   - non-streaming (we can read the body and discard)
            #   - OR streaming AND we haven't streamed any body yet (status only)
            if response.status in _RETRIABLE_STATUS and attempt < _max_retries:
                retry_after = resp_headers.get("Retry-After") or resp_headers.get("retry-after")
                # Drain & close so we can reuse the connection class
                try:
                    response.read(2048)
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
                delay = _compute_backoff(attempt, retry_after)
                logger.warning(
                    "Upstream returned %d (attempt %d/%d). Retrying in %.2fs (Retry-After=%s)",
                    response.status, attempt + 1, _max_retries + 1, delay, retry_after,
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
            # Network-level error before the response started. Safe to retry.
            last_error = e
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            if attempt < _max_retries:
                delay = _compute_backoff(attempt)
                logger.warning(
                    "Network error contacting %s://%s:%s%s (attempt %d/%d): %s. "
                    "Retrying in %.2fs",
                    scheme, target_host_clean, port, full_path,
                    attempt + 1, _max_retries + 1, e, delay,
                )
                time.sleep(delay)
                continue
            logger.error(
                "Error forwarding request to %s://%s:%s%s: %s",
                scheme, target_host_clean, port, full_path, e,
            )
            raise
        except Exception as e:
            # Anything else (programming error etc.): no retry.
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            logger.error(
                "Error forwarding request to %s://%s:%s%s: %s",
                scheme, target_host_clean, port, full_path, e,
            )
            raise

    # Exhausted retries
    if last_error is not None:
        raise last_error
    # Should not reach here, but guard anyway
    raise RuntimeError("forward_request: exhausted retries with no exception captured")


# ── Request Handler ──────────────────────────────────────────────────────────

class _DownstreamGoneError(Exception):
    """Raised when the downstream client (Claude CLI) disconnected.

    Used to distinguish 'client already gone, do NOT 502' from real upstream
    failures. ``phase`` indicates where the disconnect was detected:
      - 'write_headers'   : while sending response headers downstream
      - 'write_response'  : while writing non-stream response body
      - 'write_stream'    : while streaming SSE bytes
      - 'write_keepalive' : while writing :keep-alive ping
    """

    def __init__(self, phase, cause):
        super().__init__(f"downstream client gone (phase={phase}): {cause}")
        self.phase = phase
        self.cause = cause


class ProxyHTTPRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the proxy server."""

    # Suppress default logging (we do our own)
    def log_message(self, format, *args):
        pass

    # ── Routing ──────────────────────────────────────────────────────────

    def _send_json_response(self, status_code, data, extra_headers=None):
        """Send a JSON response."""
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_error_response(self, status_code, error_type, message):
        """Send an Anthropic-style error response."""
        self._send_json_response(status_code, {
            "type": "error",
            "error": {
                "type": error_type,
                "message": message,
            },
        })

    def _get_request_body(self):
        """Read the request body."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 0:
            return self.rfile.read(content_length)
        return b""

    def _get_model_and_config(self, body):
        """Extract model name from body and return (model_name, config_entry)."""
        if not is_loaded():
            return None, None, "Proxy configuration not loaded"

        model = extract_model_from_body(body)
        if not model:
            return None, None, "Missing 'model' field in request body"

        config_entry = get_model_config(model)
        if not config_entry:
            return None, None, f"Model '{model}' is not available in proxy configuration"

        return model, config_entry, None

    def _route_by_model(self, body, path_suffix):
        """
        Route a request based on the model field in the body.
        path_suffix: e.g., "/v1/messages" or "/v1/messages/count_tokens"
        """
        model, config_entry, error = self._get_model_and_config(body)
        if error:
            self._send_error_response(400, "invalid_request_error", error)
            return

        target_base = config_entry["ANTHROPIC_BASE_URL"].rstrip("/")
        target_path = path_suffix
        auth_token = config_entry["ANTHROPIC_AUTH_TOKEN"]

        # Determine if streaming
        is_stream = False
        tool_summary = None
        try:
            req_data = json.loads(body)
            is_stream = req_data.get("stream", False)
            tool_summary = summarize_request_tools(body)
        except (json.JSONDecodeError, TypeError):
            pass

        if tool_summary:
            logger.info("TOOL_DIAG request model=%s summary=%s", model, tool_summary)

        # Build forwarded headers
        fwd_headers = dict(self.headers)
        fwd_headers["x-api-key"] = auth_token
        fwd_headers["Authorization"] = f"Bearer {auth_token}"

        logger.info(
            "Forwarding %s %s -> model=%s target=%s%s stream=%s",
            self.command, self.path, model, target_base, target_path, is_stream,
        )

        try:
            if is_stream:
                self._handle_streaming_forward(
                    self.command, target_base, target_path,
                    fwd_headers, body, model,
                )
            else:
                self._handle_normal_forward(
                    self.command, target_base, target_path,
                    fwd_headers, body,
                )
        except _DownstreamGoneError as e:
            # Client disconnected before / during write-back. Upstream call may
            # have succeeded; this is NOT an upstream error, do not 502.
            logger.warning(
                "Downstream client gone while proxying to %s%s (phase=%s): %s",
                target_base, target_path, e.phase, e.cause,
            )
            # Cannot send_error_response because the socket is already broken.
        except Exception as e:
            logger.error(
                "Error proxying to %s%s (phase=upstream): %s",
                target_base, target_path, e,
            )
            try:
                self._send_error_response(502, "api_error", f"Upstream request failed: {str(e)}")
            except (BrokenPipeError, ConnectionResetError) as werr:
                logger.warning(
                    "Could not deliver 502 to client (already gone): %s", werr,
                )

    def _handle_normal_forward(self, method, target_base, target_path, headers, body):
        """Forward a request and return the full response.

        Raises _DownstreamGoneError if the failure happens while writing back
        to the client (so the caller can distinguish from upstream errors).
        """
        status, resp_headers, resp_body = forward_request(
            method, target_base, target_path, headers, body, stream=False,
        )

        # Best-effort repair of known-broken tool_use inputs in non-streaming
        # responses. Mirrors StreamToolInputRewriter for the streaming path.
        if _TOOL_INPUT_REPAIR_ENABLED and resp_body:
            resp_body = _repair_non_stream_body(resp_body)

        try:
            self.send_response(status)
            for key, value in resp_headers.items():
                if key.lower() not in ("transfer-encoding", "connection", "content-encoding"):
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(resp_body)
        except (BrokenPipeError, ConnectionResetError) as e:
            raise _DownstreamGoneError("write_response", e) from e

    def _handle_streaming_forward(self, method, target_base, target_path, headers, body, model):
        """
        Forward a request and stream the SSE response back to the client.

        Implementation notes (Method B — threaded reader + Queue):

          We previously tried select(sock_fd) to multiplex a keep-alive
          timer with upstream readability. That approach has a SUBTLE BUG:
          http.client.HTTPResponse uses a BufferedReader on top of the
          socket. Once readline() pulls 8KB into the buffer, subsequent
          select() calls see the OS-level socket as "not ready" even
          though there is plenty of unread SSE data in the buffer. This
          causes spurious "upstream silent" detections.

          The correct fix is to do blocking readline() in a background
          thread (which lets BufferedReader behave naturally) and use a
          thread-safe Queue to feed lines to the main loop. The main
          loop uses Queue.get(timeout=keepalive) — that is the only
          place we wait, and it has nothing to do with raw socket
          readability, so the buffer issue disappears.

          Once we've started streaming bytes to the client, we NEVER
          retry (would corrupt a billed conversation). Retries happen
          only inside forward_request() and only before the stream
          starts.
        """
        status, resp_headers, response, conn = forward_request(
            method, target_base, target_path, headers, body, stream=True,
        )

        # ── Optional raw capture for proxy-vs-direct comparison ──────────
        capture_files = None  # tuple of (req_path, hdr_path, sse_path, tgt_path, sse_fp)
        if _CAPTURE_DIR:
            try:
                import uuid
                ts = time.strftime("%Y%m%dT%H%M%S")
                reqid = uuid.uuid4().hex[:8]
                prefix = os.path.join(_CAPTURE_DIR, f"{ts}-{reqid}")
                req_p = prefix + ".req.json"
                hdr_p = prefix + ".req-headers.json"
                sse_p = prefix + ".upstream.sse"
                tgt_p = prefix + ".target.txt"
                with open(req_p, "wb") as fp:
                    fp.write(body or b"")
                masked = {}
                for k, v in headers.items():
                    if k.lower() in ("authorization", "x-api-key"):
                        sv = str(v)
                        masked[k] = (sv[:8] + "...REDACTED..." + sv[-4:]) if len(sv) > 16 else "REDACTED"
                    else:
                        masked[k] = v
                with open(hdr_p, "w") as fp:
                    json.dump(masked, fp, ensure_ascii=False, indent=2)
                with open(tgt_p, "w") as fp:
                    fp.write(f"{method} {target_base}{target_path}\n")
                sse_fp = open(sse_p, "wb")
                capture_files = (req_p, hdr_p, sse_p, tgt_p, sse_fp)
                logger.info("CAPTURE saved prefix=%s", prefix)
            except Exception as e:
                logger.warning("CAPTURE setup failed: %s", e)
                capture_files = None

        logger.info("Upstream stream response: status=%s", status)
        if status >= 400:
            # On error before streaming begins, drain a bit for diagnostics.
            try:
                err_preview = response.read(2048)
                logger.error("Upstream error body preview: %s", err_preview[:1024])
            except Exception:
                err_preview = b""
            try:
                self.send_response(status)
                for key, value in resp_headers.items():
                    kl = key.lower()
                    if kl not in ("transfer-encoding", "connection", "content-encoding", "content-length"):
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(err_preview)))
                self.end_headers()
                self.wfile.write(err_preview)
            finally:
                conn.close()
            return

        # Send response headers downstream
        try:
            self.send_response(status)
            for key, value in resp_headers.items():
                kl = key.lower()
                if kl not in ("transfer-encoding", "connection", "content-encoding", "content-length"):
                    self.send_header(key, value)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError) as e:
            logger.warning(
                "Failed sending response headers downstream (client gone): %s", e,
            )
            try:
                conn.close()
            except Exception:
                pass
            raise _DownstreamGoneError("write_headers", e) from e
        except Exception as e:
            logger.error("Failed sending response headers downstream: %s", e)
            try:
                conn.close()
            except Exception:
                pass
            return

        # ----------------------------------------------------------------
        # Background reader thread — does blocking readline() so that
        # BufferedReader works naturally. Posts each line to a Queue.
        # Sentinels:
        #   ("data", bytes)  -> a line of SSE bytes from upstream
        #   ("eof", None)    -> upstream closed cleanly
        #   ("err", Exception) -> any exception while reading upstream
        # ----------------------------------------------------------------
        line_queue: "queue.Queue" = queue.Queue(maxsize=1024)
        reader_stop = threading.Event()

        def _reader():
            try:
                while not reader_stop.is_set():
                    try:
                        line = response.readline()
                    except Exception as exc:
                        line_queue.put(("err", exc))
                        return
                    if not line:
                        line_queue.put(("eof", None))
                        return
                    if capture_files:
                        try:
                            capture_files[4].write(line)
                            capture_files[4].flush()
                        except Exception:
                            pass
                    try:
                        line_queue.put(("data", line), timeout=5)
                    except queue.Full:
                        # Downstream is too slow / stuck. Treat as fatal.
                        line_queue.put(("err", RuntimeError("downstream queue full")))
                        return
            except Exception as exc:  # belt-and-suspenders
                try:
                    line_queue.put_nowait(("err", exc))
                except Exception:
                    pass

        reader_thread = threading.Thread(
            target=_reader, name="proxy-stream-reader", daemon=True,
        )
        reader_thread.start()

        bytes_streamed = 0
        line_count = 0
        last_data_at = time.monotonic()
        last_ping_at = time.monotonic()
        keepalive_sent = 0
        client_gone = False
        exit_reason = "unknown"
        first_event_at = None
        last_err = None
        tool_diag = StreamToolDiagnostics(model)
        tool_rewriter = StreamToolInputRewriter(model)
        xml_guard = XmlLeakGuard(model)

        def _write_upstream_line(line):
            """Write exactly one upstream SSE line to Claude Code.

            The proxy's default contract is transparent Anthropic-protocol
            forwarding. Diagnostics may parse a copy of the line, but must never
            rewrite, buffer, synthesize, or drop upstream response bytes.

            Exception: when CLAUDE_PROXY_TOOL_REPAIR is on, the rewriter may
            buffer `input_json_delta` lines for known-broken tools and emit a
            patched delta at `content_block_stop`. See StreamToolInputRewriter.
            """
            nonlocal bytes_streamed, line_count
            for out_line in tool_rewriter.feed(line):
                self.wfile.write(out_line)
                bytes_streamed += len(out_line)
                line_count += 1
            self.wfile.flush()

        try:
            while True:
                # Wait up to 1s for a line — granular enough for the
                # keep-alive / idle timers, while still being efficient.
                try:
                    kind, payload = line_queue.get(timeout=1.0)
                except queue.Empty:
                    kind = "tick"
                    payload = None

                if kind == "data":
                    line = payload
                    tool_diag.feed(line)
                    # ── XML-as-text tool-call leak guard (C: always log; B: optional abort)
                    verdict = xml_guard.inspect(line)
                    if verdict.drop_line:
                        if verdict.should_abort:
                            # First detection — log loudly and decide whether
                            # to actually abort the stream.
                            info = xml_guard.trip_info or {}
                            logger.warning(
                                "XML_LEAK detected model=%s block_index=%s accum_len=%s "
                                "guard_enabled=%s snippet=%r",
                                model, info.get("block_index"),
                                info.get("accum_len"),
                                _XML_LEAK_GUARD_ENABLED,
                                info.get("snippet"),
                            )
                            if _XML_LEAK_GUARD_ENABLED:
                                # Inject synthetic overloaded_error + message_stop
                                # so Claude Code's built-in retry kicks in. Reuses
                                # the exact same shape as the upstream-idle-timeout
                                # injection path (proven safe).
                                try:
                                    err_payload = json.dumps({
                                        "type": "error",
                                        "error": {
                                            "type": "overloaded_error",
                                            "message": (
                                                "Upstream emitted a tool call as "
                                                "XML text instead of a tool_use "
                                                "block. The proxy aborted the "
                                                "stream so Claude Code can retry."
                                            ),
                                        },
                                    }, ensure_ascii=False)
                                    self.wfile.write(b"event: error\n")
                                    self.wfile.write(
                                        b"data: " + err_payload.encode("utf-8") + b"\n\n"
                                    )
                                    stop_payload = json.dumps({"type": "message_stop"})
                                    self.wfile.write(b"event: message_stop\n")
                                    self.wfile.write(
                                        b"data: " + stop_payload.encode("utf-8") + b"\n\n"
                                    )
                                    self.wfile.flush()
                                except (BrokenPipeError, ConnectionResetError) as e:
                                    logger.info(
                                        "Client gone while sending xml-leak abort: %s", e,
                                    )
                                    client_gone = True
                                    last_err = e
                                except Exception as e:
                                    logger.warning(
                                        "Failed to write xml-leak abort frame: %s", e,
                                    )
                                exit_reason = "xml_leak_guard"
                                break
                            # Guard disabled (C-only mode): just drop the
                            # offending line but keep streaming. The text
                            # block is already partially rendered downstream.
                        # drop_line=True but should_abort=False means we're
                        # post-trip and silently dropping leftover bytes.
                        last_data_at = time.monotonic()
                        last_ping_at = time.monotonic()
                        continue
                    try:
                        _write_upstream_line(line)
                    except (BrokenPipeError, ConnectionResetError) as e:
                        logger.warning(
                            "Client disconnected mid-stream "
                            "(after %d lines / %d bytes, %d keep-alives): %s",
                            line_count, bytes_streamed, keepalive_sent, e,
                        )
                        client_gone = True
                        exit_reason = "client_gone"
                        last_err = e
                        break
                    if first_event_at is None:
                        first_event_at = time.monotonic()
                    last_data_at = time.monotonic()
                    last_ping_at = time.monotonic()
                    continue

                if kind == "eof":
                    idle_before_eof = time.monotonic() - last_data_at
                    logger.info(
                        "Upstream EOF (clean close) after %d lines / %d bytes; "
                        "idle_before_eof=%.2fs",
                        line_count, bytes_streamed, idle_before_eof,
                    )
                    exit_reason = "upstream_eof"
                    break

                if kind == "err":
                    exc = payload
                    last_err = exc
                    msg = str(exc)
                    if "timed out" in msg.lower() or isinstance(exc, TimeoutError):
                        logger.error(
                            "Upstream stream read timed out "
                            "(streamed %d lines / %d bytes, %d keep-alives sent). "
                            "exc_type=%s",
                            line_count, bytes_streamed, keepalive_sent,
                            type(exc).__name__,
                        )
                        exit_reason = "upstream_read_timeout"
                    else:
                        logger.error(
                            "Streaming read error after %d lines / %d bytes "
                            "(%d keep-alives sent). exc_type=%s msg=%s",
                            line_count, bytes_streamed, keepalive_sent,
                            type(exc).__name__, msg,
                            exc_info=True,
                        )
                        exit_reason = "upstream_read_error"
                    break

                # kind == "tick": no upstream data in the last 1s.
                now = time.monotonic()

                # Hard idle bound (undici-style bodyTimeout). Only triggers
                # if upstream really stops sending, because the buffer
                # invisibility bug is fixed by the background reader.
                if now - last_data_at > _stream_timeout:
                    logger.error(
                        "Upstream stream idle >%ds (streamed %d lines / %d bytes, "
                        "%d keep-alives sent). Closing.",
                        _stream_timeout, line_count, bytes_streamed, keepalive_sent,
                    )
                    exit_reason = "stream_timeout"
                    break

                # Proactive upstream-health hint (only if configured > 0).
                # Disabled by default now that the buffer bug is gone, since
                # _stream_timeout already guards against true upstream hangs.
                if (
                    _upstream_idle_limit > 0
                    and now - last_data_at > _upstream_idle_limit
                ):
                    logger.warning(
                        "Upstream silent >%ds (no data, no ping). "
                        "Proactively closing stream "
                        "(streamed %d lines / %d bytes, %d keep-alives sent).",
                        _upstream_idle_limit,
                        line_count, bytes_streamed, keepalive_sent,
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
                        self.wfile.write(b"event: error\n")
                        self.wfile.write(b"data: " + err_payload.encode("utf-8") + b"\n\n")
                        stop_payload = json.dumps({"type": "message_stop"})
                        self.wfile.write(b"event: message_stop\n")
                        self.wfile.write(b"data: " + stop_payload.encode("utf-8") + b"\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError) as e:
                        logger.info(
                            "Client already gone while sending synthetic stop: %s", e,
                        )
                        client_gone = True
                        last_err = e
                    except Exception as e:
                        logger.warning("Failed to write synthetic stop event: %s", e)
                    exit_reason = "upstream_idle_limit"
                    break

                # Inject SSE comment ping to keep intermediaries alive
                # (some proxies / load balancers close idle SSE connections).
                if _stream_keepalive > 0 and now - last_ping_at >= _stream_keepalive:
                    try:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        keepalive_sent += 1
                        last_ping_at = now
                    except (BrokenPipeError, ConnectionResetError) as e:
                        logger.warning(
                            "Client disconnected during keep-alive "
                            "(streamed %d lines / %d bytes): %s",
                            line_count, bytes_streamed, e,
                        )
                        client_gone = True
                        exit_reason = "client_gone_on_keepalive"
                        last_err = e
                        break
        finally:
            duration = time.monotonic() - last_data_at
            logger.info(
                "Stream finished: lines=%d bytes=%d keepalives=%d "
                "idle_at_close=%.2fs client_gone=%s exit_reason=%s",
                line_count, bytes_streamed, keepalive_sent,
                max(0.0, duration), client_gone, exit_reason,
            )
            # Tell the reader thread to stop and tear down the connection.
            reader_stop.set()
            try:
                conn.close()
            except Exception:
                pass
            # Drain any leftover queue items so the reader thread can exit.
            try:
                while True:
                    line_queue.get_nowait()
            except queue.Empty:
                pass
            # Don't block server thread on join; reader is daemon.
            reader_thread.join(timeout=0.5)
            # Close the capture sink (if any).
            if capture_files:
                try:
                    capture_files[4].close()
                except Exception:
                    pass

    def _route_by_default_model(self, path):
        """Route a request using the default (first) model."""
        model_name, config_entry = get_default_model_config()
        if not config_entry:
            self._send_error_response(503, "api_error", "No default model available")
            return

        target_base = config_entry["ANTHROPIC_BASE_URL"].rstrip("/")
        target_path = path
        auth_token = config_entry["ANTHROPIC_AUTH_TOKEN"]

        body = self._get_request_body()

        fwd_headers = dict(self.headers)
        fwd_headers["x-api-key"] = auth_token

        logger.info(
            "Forwarding %s %s -> default-model=%s target=%s%s",
            self.command, self.path, model_name, target_base, target_path,
        )

        try:
            status, resp_headers, resp_body = forward_request(
                self.command, target_base, target_path, fwd_headers, body, stream=False,
            )
            if _TOOL_INPUT_REPAIR_ENABLED and resp_body:
                resp_body = _repair_non_stream_body(resp_body)
            self.send_response(status)
            for key, value in resp_headers.items():
                kl = key.lower()
                if kl not in ("transfer-encoding", "connection", "content-encoding"):
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(resp_body)
        except Exception as e:
            logger.error("Error proxying to %s%s: %s", target_base, target_path, e)
            self._send_error_response(502, "api_error", f"Upstream request failed: {str(e)}")

    # ── GET handler ──────────────────────────────────────────────────────

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/v1/models":
            self._handle_get_models()
        elif re.match(r"^/v1/messages/batches/[^/]+/results$", path):
            self._route_by_default_model(self.path)
        elif re.match(r"^/v1/messages/batches/[^/]+$", path):
            self._route_by_default_model(self.path)
        elif re.match(r"^/v1/files/[^/]+$", path):
            self._route_by_default_model(self.path)
        elif path == "/v1/files":
            self._route_by_default_model(self.path)
        elif path == "/admin/reload":
            self._handle_admin_reload()
        else:
            self._send_error_response(404, "not_found_error", f"Not found: {path}")

    # ── POST handler ─────────────────────────────────────────────────────

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        # Preserve original query string when forwarding
        path_with_query = self.path

        if path == "/v1/messages":
            body = self._get_request_body()
            self._route_by_model(body, path_with_query)
        elif path == "/v1/messages/count_tokens":
            body = self._get_request_body()
            self._route_by_model(body, path_with_query)
        elif path == "/v1/messages/batches":
            body = self._get_request_body()
            model = extract_model_from_batch_body(body)
            if model and get_model_config(model):
                self._route_by_model(body, path_with_query)
            else:
                self._route_by_default_model(path_with_query)
        elif path == "/v1/files":
            self._route_by_default_model(path_with_query)
        elif path == "/admin/reload":
            self._handle_admin_reload()
        else:
            self._send_error_response(404, "not_found_error", f"Not found: {path}")

    # ── DELETE handler ───────────────────────────────────────────────────

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if re.match(r"^/v1/files/[^/]+$", path):
            self._route_by_default_model(self.path)
        else:
            self._send_error_response(404, "not_found_error", f"Not found: {path}")

    # ── OPTIONS handler (CORS preflight) ─────────────────────────────────

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, x-api-key, anthropic-version, anthropic-beta")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    # ── Specific handlers ────────────────────────────────────────────────

    def _handle_get_models(self):
        """Handle GET /v1/models - return available models from config."""
        if not is_loaded():
            self._send_error_response(503, "api_error", "Proxy configuration not loaded")
            return

        response = build_models_response()
        model_count = len(response["data"])
        logger.info("GET /v1/models -> 200 OK (%d model(s))", model_count)
        self._send_json_response(200, response)

    def _handle_admin_reload(self):
        """Reload config.json AND sync ~/.claude/settings.json."""
        success = load_config()
        if not success:
            response = {
                "status": "error",
                "message": f"Failed to reload config from {_config_path}",
            }
            logger.error("Admin reload -> 500 ERROR")
            self._send_json_response(500, response)
            return

        model_list = list(get_config().keys())
        sync_ok, sync_info = sync_claude_settings()

        response = {
            "status": "ok" if sync_ok else "partial",
            "message": (
                "Configuration reloaded successfully"
                if sync_ok
                else "Config reloaded but Claude settings sync failed"
            ),
            "models": model_list,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "claude_settings_sync": {"ok": sync_ok, **sync_info},
        }
        if sync_ok:
            logger.info(
                "Admin reload -> 200 OK (models: %s, settings synced: %s)",
                ", ".join(model_list), sync_info.get("path"),
            )
        else:
            logger.warning(
                "Admin reload -> 200 PARTIAL (models: %s, sync error: %s)",
                ", ".join(model_list), sync_info.get("error"),
            )
        self._send_json_response(200, response)


# ── Server ───────────────────────────────────────────────────────────────────

def run_server(host="0.0.0.0", port=8080, config_path="config.json"):
    """Start the proxy server."""
    global _config_path, _server_host, _server_port
    _config_path = config_path
    _server_host = host
    _server_port = port

    # Load initial configuration
    if not load_config():
        logger.warning("Initial configuration could not be loaded. Use /admin/reload to retry.")
    else:
        sync_ok, sync_info = sync_claude_settings()
        if sync_ok:
            logger.info(
                "Claude settings synced on startup: %s (mapping=%s)",
                sync_info.get("path"), sync_info.get("mapping"),
            )
        else:
            logger.warning("Claude settings sync skipped: %s", sync_info.get("error"))

    # Use ThreadingHTTPServer so a single long-running upstream request
    # (e.g. a 5-minute 504 retry chain on a non-stream POST) cannot block the
    # whole proxy. Each incoming connection runs on its own daemon thread,
    # which is what Claude Code expects from any Anthropic-compatible server.
    # See incident 2026-06-30 18:26: a stuck non-stream retry chain caused
    # ConnectionRefused on the TUI because the single accept loop was wedged.
    server = ThreadingHTTPServer((host, port), ProxyHTTPRequestHandler)
    # Make sure Python doesn't wait for in-flight handler threads to finish
    # on Ctrl-C — they may be in a 5-minute upstream wait.
    server.daemon_threads = True
    logger.info("=" * 60)
    logger.info("Claude Code API Proxy Server (threaded)")
    logger.info("Listening on http://%s:%d", host, port)
    logger.info("Configuration: %s", os.path.abspath(_config_path))
    logger.info("Endpoints:")
    logger.info("  GET  /v1/models                  - List available models")
    logger.info("  POST /v1/messages                - Send message (streaming supported)")
    logger.info("  POST /v1/messages/count_tokens   - Count tokens")
    logger.info("  POST /v1/messages/batches        - Batch messages")
    logger.info("  GET  /v1/messages/batches/{id}   - Batch status")
    logger.info("  GET  /v1/messages/batches/{id}/results - Batch results")
    logger.info("  POST /v1/files                   - Upload file")
    logger.info("  GET  /v1/files                   - List files")
    logger.info("  GET  /v1/files/{id}              - Get file")
    logger.info("  DELETE /v1/files/{id}            - Delete file")
    logger.info("  POST/GET /admin/reload           - Hot-reload config")
    logger.info("=" * 60)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down server...")
        server.server_close()
        logger.info("Server stopped.")


def main():
    parser = argparse.ArgumentParser(description="Claude Code API Proxy Server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PROXY_PORT", 8080)),
                        help="Port to listen on (default: 8080, env: PROXY_PORT)")
    parser.add_argument("--config", type=str, default=os.environ.get("PROXY_CONFIG", "config.json"),
                        help="Path to config file (default: config.json, env: PROXY_CONFIG)")
    parser.add_argument("--host", type=str, default="0.0.0.0",
                        help="Host to bind to (default: 0.0.0.0)")
    parser.add_argument(
        "--log-dir", type=str,
        default=os.environ.get("PROXY_LOG_DIR", str(_DEFAULT_LOG_DIR)),
        help=f"Directory for rotating log files (default: {_DEFAULT_LOG_DIR}, env: PROXY_LOG_DIR)",
    )
    parser.add_argument(
        "--log-max-bytes", type=int,
        default=int(os.environ.get("PROXY_LOG_MAX_BYTES", _DEFAULT_LOG_MAX_BYTES)),
        help=f"Max bytes per log file before rotation (default: {_DEFAULT_LOG_MAX_BYTES}, ~100MB)",
    )
    parser.add_argument(
        "--log-backup-count", type=int,
        default=int(os.environ.get("PROXY_LOG_BACKUP_COUNT", _DEFAULT_LOG_BACKUP_COUNT)),
        help=f"Number of rotated log files to keep (default: {_DEFAULT_LOG_BACKUP_COUNT})",
    )
    parser.add_argument(
        "--http-timeout", type=int,
        default=int(os.environ.get("PROXY_HTTP_TIMEOUT", _DEFAULT_HTTP_TIMEOUT)),
        help=f"Socket timeout (s) for non-streaming upstream calls "
             f"(default: {_DEFAULT_HTTP_TIMEOUT}, env: PROXY_HTTP_TIMEOUT)",
    )
    parser.add_argument(
        "--stream-timeout", type=int,
        default=int(os.environ.get("PROXY_STREAM_TIMEOUT", _DEFAULT_STREAM_TIMEOUT)),
        help=f"Socket idle timeout (s) for streaming SSE upstream calls. "
             f"Increase this if upstream takes a long time between chunks "
             f"(default: {_DEFAULT_STREAM_TIMEOUT}, env: PROXY_STREAM_TIMEOUT)",
    )
    parser.add_argument(
        "--stream-keepalive", type=int,
        default=int(os.environ.get("PROXY_STREAM_KEEPALIVE", _DEFAULT_STREAM_KEEPALIVE)),
        help=f"Seconds between proxy-injected SSE ':keep-alive' pings while "
             f"upstream is silent (default: {_DEFAULT_STREAM_KEEPALIVE}, env: PROXY_STREAM_KEEPALIVE)",
    )
    parser.add_argument(
        "--max-retries", type=int,
        default=int(os.environ.get("PROXY_MAX_RETRIES", _DEFAULT_MAX_RETRIES)),
        help=f"Max retries for transient upstream errors (network / 408 / 409 / 429 / 5xx). "
             f"Streams that have already begun are NEVER retried. "
             f"(default: {_DEFAULT_MAX_RETRIES}, env: PROXY_MAX_RETRIES)",
    )
    parser.add_argument(
        "--upstream-idle-limit", type=int,
        default=int(os.environ.get("PROXY_UPSTREAM_IDLE_LIMIT", _DEFAULT_UPSTREAM_IDLE_LIMIT)),
        help=f"If upstream stops sending ANY bytes for this many seconds during a "
             f"stream (no chunk, no ping), proactively close the connection. "
             f"Set to 0 to disable. "
             f"(default: {_DEFAULT_UPSTREAM_IDLE_LIMIT}, env: PROXY_UPSTREAM_IDLE_LIMIT)",
    )

    args = parser.parse_args()

    # Configure rotating file logging before starting the server
    setup_file_logging(
        log_dir=args.log_dir,
        max_bytes=args.log_max_bytes,
        backup_count=args.log_backup_count,
    )

    # Apply timeout / retry settings
    configure_timeouts(
        http_timeout=args.http_timeout,
        stream_timeout=args.stream_timeout,
        stream_keepalive=args.stream_keepalive,
        max_retries=args.max_retries,
        upstream_idle_limit=args.upstream_idle_limit,
    )
    logger.info(
        "Forward tunables: http-timeout=%ds stream-idle=%ds stream-keepalive=%ds "
        "upstream-idle-limit=%ds max-retries=%d",
        args.http_timeout, args.stream_timeout, args.stream_keepalive,
        args.upstream_idle_limit, args.max_retries,
    )

    run_server(host=args.host, port=args.port, config_path=args.config)


if __name__ == "__main__":
    main()
