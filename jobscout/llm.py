"""LLM transport.

Three interchangeable backends:

``cli`` (default)
    Shells out to the ``claude`` CLI in headless mode, so calls are billed to the
    Claude account you are already logged into — no separate API key, and the
    hosted ``WebSearch`` / ``WebFetch`` tools come along for free, which is what
    makes live job discovery possible at all.

``anthropic``
    The Anthropic SDK with ``ANTHROPIC_API_KEY``, the same arrangement
    covered-call-app uses. Kept as a fallback for machines with no CLI login.

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


class AnthropicBackend(Backend):
    """The Anthropic SDK, using ANTHROPIC_API_KEY (billed to the API account)."""

    name = "anthropic"

    def __init__(self, api_key: Optional[str] = None) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise LLMError("ANTHROPIC_API_KEY is not set; use JOBSCOUT_BACKEND=cli "
                           "to bill your Claude CLI login instead.")
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise LLMError("the `anthropic` package is not installed "
                           "(pip install anthropic)") from exc
        self._client = anthropic.Anthropic(api_key=key, timeout=600.0, max_retries=2)

    def complete(self, prompt: str, *, model: str, system: str = "",
                 tools: Sequence[str] = (), timeout: int = 600) -> Response:
        kwargs: Dict[str, Any] = {
            "model": model,
            "max_tokens": 8000,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        if tools:
            # The server-side search tool; the SDK runs the loop for us.
            kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search",
                                "max_uses": 8}]
        message = self._client.messages.create(**kwargs)
        text = "".join(block.text for block in message.content
                       if getattr(block, "type", "") == "text")
        server_use = getattr(message.usage, "server_tool_use", None)
        searches = int(getattr(server_use, "web_search_requests", 0) or 0)
        return Response(text=text, cost_usd=0.0, web_searches=searches)


class MockBackend(Backend):
    """Canned responses keyed by a marker the prompt carries."""

    name = "mock"

    def __init__(self, responses: Optional[Dict[str, str]] = None,
                 default: str = "{}") -> None:
        self.responses = responses or {}
        self.default = default
        self.prompts: List[str] = []

    def complete(self, prompt: str, *, model: str, system: str = "",
                 tools: Sequence[str] = (), timeout: int = 600) -> Response:
        self.prompts.append(prompt)
        for marker, reply in self.responses.items():
            if marker in prompt:
                return Response(text=reply)
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
