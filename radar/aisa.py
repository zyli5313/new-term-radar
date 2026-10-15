"""Client for AIsa, the radar's one paid dependency: its tool router (X posts, Google
Trends and whois data through DataForSEO) and its OpenAI-compatible chat endpoint (the
name judge). One key: AISA_API_KEY, or the one the AIsa CLI stores when you log in.

The tool router runs a batch's calls one by one, about 10 s each, so batches are kept to
BATCH calls and the timeout is long. Its nginx front answers 504 when a batch runs long
(2026-10-14: one of seven X batches, and call_all threw the other six away). So a batch
that fails with a 5xx or a dropped connection is sent once more, and a batch that still
fails costs only its own calls: call_all keeps every other batch's answers. Every call is
billed, so the retry is once and never for a 4xx.

Prices seen in October 2026: an account's last 20 posts $0.003, one Trends task $0.0012,
one whois overview query about $0.13.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from . import UA

ROUTER = os.environ.get("AISA_ROUTER_BASE_URL", "https://tools.aisa.one") + "/v1/tool-router/aisa-batch-use"
CHAT = os.environ.get("AISA_API_BASE_URL", "https://api.aisa.one") + "/v1/chat/completions"
MAX_CALLS = 20      # the router's own limit per batch
BATCH = 3           # what call_all sends at once, about 30 s a batch (5 drew a 504)
TRIES = 2           # a batch that fails with a 5xx or a dropped connection is sent once more
TIMEOUT = 240
CHAT_TIMEOUT = 90


class AisaError(RuntimeError):
    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient   # a 5xx or a dropped connection: worth sending again


def key() -> str:
    """AISA_API_KEY, else the key the AIsa CLI stored at login (~/.aisa)."""
    k = os.environ.get("AISA_API_KEY", "").strip()
    if k:
        return k
    home = Path.home() / ".aisa"
    try:
        if (home / "key").exists():
            return (home / "key").read_text().strip()
        if (home / "tokens.json").exists():
            return json.loads((home / "tokens.json").read_text()).get("accessToken", "") or _no_key()
    except (OSError, ValueError):
        pass
    return _no_key()


def _no_key() -> str:
    raise AisaError("no AIsa key: set AISA_API_KEY, or log in with the AIsa CLI (`aisa login`)")


def _post(url: str, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {key()}",
        "User-Agent": UA, "x-aisa-source": "new-term-radar"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.load(res)
    except urllib.error.HTTPError as exc:
        raise AisaError(f"HTTP {exc.code}: {' '.join(exc.read().decode('utf-8', 'replace').split())[:200]}",
                        transient=exc.code >= 500) from exc
    except OSError as exc:
        raise AisaError(f"{type(exc).__name__}: {exc}", transient=True) from exc
    except ValueError as exc:
        raise AisaError(f"{type(exc).__name__}: {exc}") from exc


def call(calls: list[dict]) -> tuple[dict[str, dict], int]:
    """Run one batch of tool calls. Returns ({call_id: data}, cost in micro-USD). Calls
    that failed are left out of the dict; only a failed request raises."""
    if not calls:
        return {}, 0
    if len(calls) > MAX_CALLS:
        raise AisaError(f"{len(calls)} calls in one batch, the cap is {MAX_CALLS}")
    body = _post(ROUTER, {"calls": calls}, TIMEOUT)
    if "error" in body:
        raise AisaError(str(body["error"].get("message") or body["error"])[:200])
    out, cost = {}, 0
    for r in body.get("results", []):
        cost += int(r.get("customer_cost_micros_usd") or 0)
        if r.get("successful"):
            out[r["call_id"]] = r.get("data") or {}
    return out, cost


def _call_retrying(calls: list[dict]) -> tuple[dict[str, dict], int]:
    for attempt in range(TRIES):
        try:
            return call(calls)
        except AisaError as exc:
            if not exc.transient or attempt == TRIES - 1:
                raise
    raise AssertionError("unreachable")


def call_all(calls: list[dict]) -> tuple[dict[str, dict], int]:
    """call() in batches of BATCH. A batch that still fails after its retry leaves its
    calls out of the dict, like calls the router itself failed; only when every batch
    fails does this raise, so the caller can report the error."""
    out, cost, error = {}, 0, None
    for i in range(0, len(calls), BATCH):
        try:
            data, c = _call_retrying(calls[i:i + BATCH])
        except AisaError as exc:
            error = exc
            continue
        out.update(data)
        cost += c
    if error and not out:
        raise error
    return out, cost


def chat(model: str, system: str, user: str, max_tokens: int = 1500) -> str:
    body = _post(CHAT, {"model": model, "temperature": 0, "max_tokens": max_tokens, "messages": [
        {"role": "system", "content": system}, {"role": "user", "content": user}]}, CHAT_TIMEOUT)
    try:
        return body["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise AisaError(f"unexpected chat answer: {str(body)[:160]}") from exc
