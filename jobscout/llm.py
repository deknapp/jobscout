"""LLM transport.

Three interchangeable backends:

``cli`` (default)
    Shells out to the ``claude`` CLI in headless mode, so calls are billed to the
    Claude account you are already logged into — no separate API key, and the
    hosted ``WebSearch`` / ``WebFetch`` tools come along for free, which is what
    makes live job discovery possible at all.

``anthropic``
    The Anthropic SDK with ``ANTHROPIC_API_KEY``, the same arrangement
    covered-call-app uses. Use this when the CLI login has hit its usage
    limit: a Claude subscription and an API account draw on entirely separate
    balances, so a run that stops with "usage limit reached" under ``cli`` will
    go through here. It also gets the hosted web search and web fetch tools, so
    nothing about discovery is lost — only the billing address changes.

``mock``
    Deterministic canned answers so the whole pipeline — and the test suite —
    runs offline at zero cost.

Whichever backend is active, the agents only ever ask for **JSON**, and every
response is parsed and validated here before it reaches the pipeline.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

WEB_TOOLS = ("WebSearch", "WebFetch")


class LLMError(RuntimeError):
    pass


@dataclass
class Response:
    text: str
    cost_usd: float = 0.0
    web_searches: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Usage:
    calls: int = 0
    cost_usd: float = 0.0
    web_searches: int = 0

    def add(self, response: Response) -> None:
        self.calls += 1
        self.cost_usd += response.cost_usd
        self.web_searches += response.web_searches

    def summary(self) -> str:
        return "%d model call(s), %d web search(es), $%.4f" % (
            self.calls, self.web_searches, self.cost_usd)


# --- JSON handling ---------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Any:
    """Pull the first JSON value out of a model response.

    Models wrap JSON in prose or code fences no matter how firmly you ask them
    not to, so this tries the whole string, then fenced blocks, then a balanced
    scan from the first brace or bracket.
    """
    candidates: List[str] = [text.strip()]
    candidates.extend(m.strip() for m in _FENCE.findall(text))
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:index + 1])
                    break
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise LLMError("no JSON found in model response:\n%s" % text[:800])


# --- backends --------------------------------------------------------------

class Backend:
    name = "base"

    def complete(self, prompt: str, *, model: str, system: str = "",
                 tools: Sequence[str] = (), timeout: int = 600) -> Response:
        raise NotImplementedError


class ClaudeCLIBackend(Backend):
    """``claude -p`` in headless mode, authenticated as your CLI login."""

    name = "cli"

    def __init__(self, executable: Optional[str] = None) -> None:
        self.executable = executable or shutil.which("claude") or "claude"
        if not shutil.which(self.executable):
            raise LLMError(
                "the `claude` CLI is not on PATH. Install Claude Code and run "
                "`claude` once to log in, or set JOBSCOUT_BACKEND=anthropic and "
                "provide ANTHROPIC_API_KEY.")

    def complete(self, prompt: str, *, model: str, system: str = "",
                 tools: Sequence[str] = (), timeout: int = 600) -> Response:
        argv = [self.executable, "-p", "--model", model, "--output-format", "json"]
        if tools:
            # Headless mode denies anything not listed, which is exactly the
            # sandbox we want: these agents get the web and nothing else.
            argv.append("--allowedTools")
            argv.extend(tools)
        else:
            argv.extend(["--allowedTools", "none"])
        if system:
            argv.extend(["--append-system-prompt", system])

        # Run somewhere empty so the agent has no repo or home files in reach.
        with tempfile.TemporaryDirectory(prefix="jobscout-") as workdir:
            try:
                proc = subprocess.run(
                    argv, input=prompt, capture_output=True, text=True,
                    timeout=timeout, cwd=workdir,
                    env=dict(os.environ, CLAUDE_CODE_DISABLE_TERMINAL_TITLE="1"),
                )
            except subprocess.TimeoutExpired as exc:
                raise LLMError("claude CLI timed out after %ss" % timeout) from exc

        if proc.returncode != 0:
            raise LLMError("claude CLI failed (exit %d): %s"
                           % (proc.returncode, (proc.stderr or proc.stdout)[:600]))
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise LLMError("claude CLI returned non-JSON output: %s"
                           % proc.stdout[:600]) from exc
        if payload.get("is_error"):
            raise LLMError("claude CLI reported an error: %s"
                           % str(payload.get("result"))[:600])

        searches = 0
        for usage in (payload.get("modelUsage") or {}).values():
            searches += int(usage.get("webSearchRequests") or 0)
        return Response(
            text=str(payload.get("result") or ""),
            cost_usd=float(payload.get("total_cost_usd") or 0.0),
            web_searches=searches,
            raw=payload,
        )


# Per-model API differences. The 2026-02-09 server tools carry dynamic
# filtering and only run on Opus 4.6+ / Sonnet 4.6+; older models take the
# basic variants. Server-side refusal fallbacks are an Opus 5 / Fable feature.
# A model that is not listed gets the conservative older shapes.
_MODEL_CAPS = {
    "claude-opus-5":    {"tools": "20260209", "fallbacks": True},
    "claude-opus-4-8":  {"tools": "20260209", "fallbacks": False},
    "claude-sonnet-5":  {"tools": "20260209", "fallbacks": False},
    "claude-haiku-4-5": {"tools": "basic", "fallbacks": False},
}
_DEFAULT_CAPS = {"tools": "basic", "fallbacks": False}

# USD per million tokens, first-party API rates. Only used to report what a run
# cost — an unlisted model reports 0.0 rather than a guessed number, because a
# wrong figure in the budget line is worse than an obviously missing one.
_PRICES = {
    "claude-opus-5":    (5.0, 25.0),
    "claude-opus-4-8":  (5.0, 25.0),
    "claude-sonnet-5":  (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5-1": (10.0, 50.0),
}


def _caps(model: str) -> Dict[str, Any]:
    return _MODEL_CAPS.get(model, _DEFAULT_CAPS)


def _server_tools(caps: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The API-side equivalents of the CLI's WebSearch and WebFetch."""
    if caps["tools"] == "20260209":
        search, fetch = "web_search_20260209", "web_fetch_20260209"
    else:
        search, fetch = "web_search_20250305", "web_fetch_20250910"
    return [
        {"type": search, "name": "web_search", "max_uses": 8},
        {"type": fetch, "name": "web_fetch", "max_uses": 8},
    ]


def _cost_usd(model: str, usage: Any) -> float:
    """What this call cost, from the token counts the API reports.

    Cache reads bill at a tenth of the input rate and cache writes at 1.25x;
    jobscout does not cache today, but the arithmetic is here so the budget
    line stays honest if it starts to. Web searches bill per search on top of
    this and are counted separately.
    """
    price = _PRICES.get(model)
    if not price:
        return 0.0
    in_rate, out_rate = price
    got = lambda name: float(getattr(usage, name, 0) or 0)
    return (got("input_tokens") * in_rate
            + got("cache_read_input_tokens") * in_rate * 0.1
            + got("cache_creation_input_tokens") * in_rate * 1.25
            + got("output_tokens") * out_rate) / 1_000_000.0


class AnthropicBackend(Backend):
    """The Anthropic SDK, billed to your API account rather than a CLI login.

    This is the backend to use once a Claude subscription hits its usage limit:
    `claude -p` and the API draw on entirely different balances.
    """

    name = "anthropic"

    def __init__(self, api_key: Optional[str] = None) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise LLMError("the `anthropic` package is not installed "
                           "(pip install anthropic)") from exc
        self._anthropic = anthropic
        # An unset ANTHROPIC_API_KEY does not mean there are no credentials —
        # the SDK also resolves ANTHROPIC_AUTH_TOKEN and an `ant auth login`
        # profile. So let it look, and report the problem when a call actually
        # comes back unauthorised, rather than refusing to start.
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        # Granular timeouts, not one number. A stalled connection and a genuinely
        # slow answer look identical if you only measure total elapsed time, and
        # the employer search legitimately runs for minutes: on the run of
        # 2026-09-04 four of the five search angles took 254-545s, so any total
        # cap tight enough to catch a stall would also kill real work. `read` is
        # the gap between bytes, which is the thing that actually distinguishes
        # them — but only when the response is streamed, which is why
        # `complete` streams. A hung call now fails in ~2 minutes rather than
        # 30, and a slow one is left alone.
        kwargs: Dict[str, Any] = {
            "timeout": anthropic.Timeout(3600.0, connect=15.0, read=120.0,
                                         write=60.0),
            "max_retries": 2,
        }
        if key:
            kwargs["api_key"] = key
        self._client = anthropic.Anthropic(**kwargs)

    def complete(self, prompt: str, *, model: str, system: str = "",
                 tools: Sequence[str] = (), timeout: int = 600) -> Response:
        caps = _caps(model)
        kwargs: Dict[str, Any] = {
            "model": model,
            # Room to finish. A truncated reply is unparseable JSON, which
            # reads downstream as a model failure rather than a short ceiling.
            "max_tokens": 16000,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = _server_tools(caps)

        # `timeout` from settings is the ceiling for the whole call; the
        # per-read stall detection set on the client stays as it is.
        client = self._client.with_options(
            timeout=self._anthropic.Timeout(float(timeout), connect=15.0,
                                            read=120.0, write=60.0))
        try:
            # Streamed, always. At 16k max_tokens a non-streamed request can
            # outlive an HTTP timeout, and without a byte-by-byte read timeout
            # there is nothing to distinguish a stalled connection from a long
            # answer until the whole request budget is gone.
            if caps["fallbacks"]:
                # If a safety classifier declines, the API re-runs the same
                # request on a fallback model inside the same call instead of
                # returning nothing. A decline before any output is not billed.
                with client.beta.messages.stream(
                        betas=["server-side-fallback-2026-07-01"],
                        fallbacks="default", **kwargs) as stream:
                    message = stream.get_final_message()
            else:
                with client.messages.stream(**kwargs) as stream:
                    message = stream.get_final_message()
        except self._anthropic.AuthenticationError as exc:
            raise LLMError(
                "the Anthropic API rejected your credentials. Set "
                "ANTHROPIC_API_KEY in jobscout's .env (console.anthropic.com "
                "→ API keys), or set JOBSCOUT_BACKEND=cli to bill your Claude "
                "subscription instead.") from exc
        except self._anthropic.RateLimitError as exc:
            raise LLMError("the Anthropic API rate-limited this call; lower "
                           "JOBSCOUT_MAX_WORKERS or retry shortly") from exc
        except self._anthropic.APIStatusError as exc:
            raise LLMError("the Anthropic API failed (HTTP %s): %s"
                           % (exc.status_code, str(exc)[:400])) from exc
        except self._anthropic.APITimeoutError as exc:
            raise LLMError(
                "the Anthropic API stopped responding mid-answer and the call "
                "was abandoned; the run continues without this step") from exc
        except self._anthropic.APIConnectionError as exc:
            raise LLMError("could not reach the Anthropic API: %s"
                           % str(exc)[:300]) from exc

        # A refusal is an HTTP 200 with no usable content, so it has to be
        # checked before reading the blocks or it looks like an empty answer.
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise LLMError("the model declined this request (%s): %s" % (
                getattr(details, "category", None) or "unspecified",
                getattr(details, "explanation", "") or "no explanation given"))
        if message.stop_reason == "max_tokens":
            raise LLMError("the model hit the %d-token output ceiling, so its "
                           "reply is truncated and cannot be parsed"
                           % kwargs["max_tokens"])

        text = "".join(block.text for block in message.content
                       if getattr(block, "type", "") == "text")
        server_use = getattr(message.usage, "server_tool_use", None)
        searches = (int(getattr(server_use, "web_search_requests", 0) or 0)
                    + int(getattr(server_use, "web_fetch_requests", 0) or 0))
        return Response(text=text, cost_usd=_cost_usd(model, message.usage),
                        web_searches=searches)


class MockBackend(Backend):
    """Canned responses keyed by a marker the prompt carries.

    A response may be a string, or a callable taking the prompt — the callable
    form lets a test answer per-posting (echoing back a location, say) rather
    than giving every call the same reply.
    """

    name = "mock"

    def __init__(self, responses: Optional[Dict[str, Any]] = None,
                 default: str = "{}") -> None:
        self.responses = responses or {}
        self.default = default
        self.prompts: List[str] = []

    def complete(self, prompt: str, *, model: str, system: str = "",
                 tools: Sequence[str] = (), timeout: int = 600) -> Response:
        self.prompts.append(prompt)
        for marker, reply in self.responses.items():
            if marker in prompt:
                return Response(text=reply(prompt) if callable(reply) else reply)
        return Response(text=self.default)


class LLM:
    """Backend-agnostic front door used by every agent."""

    def __init__(self, backend: Backend, *, model_cheap: str, model_strong: str,
                 timeout: int = 600) -> None:
        self.backend = backend
        self.model_cheap = model_cheap
        self.model_strong = model_strong
        self.timeout = timeout
        self.usage = Usage()

    @classmethod
    def from_settings(cls, settings) -> "LLM":
        if settings.backend == "cli":
            backend: Backend = ClaudeCLIBackend()
        elif settings.backend == "anthropic":
            backend = AnthropicBackend()
        else:
            backend = MockBackend()
        return cls(backend, model_cheap=settings.model_cheap,
                   model_strong=settings.model_strong,
                   timeout=settings.timeout_seconds)

    def ask_json(self, prompt: str, *, strong: bool = False, system: str = "",
                 web: bool = False, retries: int = 1) -> Any:
        model = self.model_strong if strong else self.model_cheap
        tools = WEB_TOOLS if web else ()
        attempt = 0
        last_error: Optional[Exception] = None
        text = ""
        while attempt <= retries:
            ask = prompt if attempt == 0 else (
                prompt + "\n\nYour previous reply could not be parsed as JSON. "
                         "Reply with the JSON value ONLY — no prose, no code fence.")
            response = self.backend.complete(ask, model=model, system=system,
                                             tools=tools, timeout=self.timeout)
            self.usage.add(response)
            text = response.text
            try:
                return extract_json(text)
            except LLMError as exc:
                last_error = exc
                attempt += 1
        raise LLMError("model did not return usable JSON after %d attempt(s): %s"
                       % (retries + 1, last_error))
