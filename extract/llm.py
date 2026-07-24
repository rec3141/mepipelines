"""LLM client for extraction and adjudication.

Two providers, one OpenAI-compatible interface:

  local       LM Studio on localhost. Free, private, no rate limit. The default.
  openrouter  Any hosted model, for adjudication or for checking a local model's work.

Every call returns a `Completion` carrying not just the text but the **provenance** — provider,
exact model ID, prompt version, token usage. That is not bookkeeping for its own sake: the catalog
schema requires `vetting.adjudication.model` and `.prompt_version` on every LLM judgment so a
judgment can be traced, compared across models, and re-derived when the model changes. A call path
that loses this is a bug.

Configuration is entirely by environment (see .env.example):

    MEP_PROVIDER          local | openrouter          (default: local)
    MEP_MODEL             model ID for that provider  (default: provider-specific)
    MEP_LOCAL_BASE_URL    default http://localhost:1234/v1
    OPENROUTER_API_KEY    required only when provider=openrouter

    python extract/llm.py --check           # probe the endpoint, list models
    python extract/llm.py --prompt "hello"  # one-shot smoke test
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Any

try:
    from openai import OpenAI, APIError, APIConnectionError
except ImportError:
    sys.exit("openai required: pip install -r requirements.txt")


# Provider defaults. `model` is a fallback only — MEP_MODEL always wins, and for the local
# provider `resolve_model()` prefers whatever LM Studio actually has loaded over any default.
PROVIDERS = {
    "local": {
        "base_url": "http://localhost:1234/v1",
        "api_key_env": None,          # LM Studio ignores the key but the SDK requires one
        "model": "google/gemma-4-26b-a4b",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "model": "anthropic/claude-sonnet-5",
    },
}

# Reference model IDs for the hosted tier. OpenRouter namespaces every model as `vendor/model`;
# the bare IDs on the right are the first-party Anthropic API names. Verify the exact OpenRouter
# slug against https://openrouter.ai/models before relying on one — OpenRouter's naming can lag
# or alias a vendor release, and a wrong slug fails at request time, not at import.
HOSTED_MODEL_REFERENCE = {
    "anthropic/claude-opus-5": "claude-opus-5",
    "anthropic/claude-sonnet-5": "claude-sonnet-5",
    "anthropic/claude-haiku-4-5": "claude-haiku-4-5",
    "anthropic/claude-fable-5": "claude-fable-5",
}

MAX_RETRIES = 4
RETRY_BASE_DELAY = 3.0

# Reasoning delimiters differ by model family and are NOT all <think>:
#   <think> … </think>                     DeepSeek-R1, QwQ, most Qwen reasoning builds
#   <|channel>thought … <channel|>         Gemma 4 (see its LM Studio model.yaml
#                                          `llm.prediction.reasoning.parsing` start/end strings)
# LM Studio normally strips these server-side, but only when its reasoning-parsing config matches
# the model; another backend, a raw GGUF, or a mismatched template will leak them into `content`.
# A stripper that only knows <think> silently passes a wall of Gemma reasoning to json.loads().
_THINK_PAIRS = [
    (re.compile(r"<think>.*?</think>\s*", re.DOTALL), re.compile(r"<think>.*", re.DOTALL)),
    (
        re.compile(r"<\|channel>thought.*?<channel\|>\s*", re.DOTALL),
        re.compile(r"<\|channel>thought.*", re.DOTALL),
    ),
]


def strip_think(text: str) -> str:
    """Remove reasoning blocks in any known delimiter style.

    Closed blocks go first, then any unclosed remainder left by a response that was cut off
    mid-thought.
    """
    if not text:
        return ""
    for closed, unclosed in _THINK_PAIRS:
        text = unclosed.sub("", closed.sub("", text))
    return text.strip()


class TruncatedResponse(RuntimeError):
    """The model hit the token ceiling before finishing.

    Raised in preference to letting an empty or half-written response reach the JSON parser, where
    it surfaces as an inscrutable "could not parse JSON from response: ''". Reasoning models are
    the usual cause: they can spend the entire budget thinking and emit zero content tokens.
    """


@dataclass
class Completion:
    """A model response plus everything needed to attribute and reproduce it."""

    text: str
    provider: str
    model: str
    prompt_version: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0    # thinking tokens, when the backend reports them separately
    finish_reason: str = ""
    elapsed_s: float = 0.0
    parsed: Any = None           # populated when a schema was supplied
    raw_text: str = ""           # pre-<think>-stripping, for debugging

    def provenance(self) -> dict:
        """The subset that belongs in a catalog record's `vetting.adjudication` block."""
        return {
            "model": f"{self.provider}:{self.model}",
            "prompt_version": self.prompt_version,
        }

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class LLMClient:
    provider: str = field(default_factory=lambda: os.getenv("MEP_PROVIDER", "local"))
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    timeout: float = 600.0

    def __post_init__(self):
        if self.provider not in PROVIDERS:
            raise ValueError(
                f"unknown provider {self.provider!r}; expected one of {sorted(PROVIDERS)}"
            )
        spec = PROVIDERS[self.provider]

        if self.base_url is None:
            env = "MEP_LOCAL_BASE_URL" if self.provider == "local" else "MEP_OPENROUTER_BASE_URL"
            self.base_url = os.getenv(env, spec["base_url"])

        if self.api_key is None:
            key_env = spec["api_key_env"]
            if key_env:
                self.api_key = os.getenv(key_env)
                if not self.api_key:
                    raise RuntimeError(
                        f"provider {self.provider!r} needs {key_env} set in the environment"
                    )
            else:
                self.api_key = "not-needed"

        # OpenRouter attributes requests to an app when these headers are present. Harmless
        # elsewhere; the SDK sends them on every request.
        headers = {}
        if self.provider == "openrouter":
            headers = {
                "HTTP-Referer": "https://github.com/rec3141/mepipelines",
                "X-Title": "mepipelines",
            }

        self._client = OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout,
            default_headers=headers or None,
        )
        self.model = self.model or self.resolve_model()

    # -- discovery ---------------------------------------------------------

    def list_models(self) -> list[str]:
        return [m.id for m in self._client.models.list().data]

    def resolve_model(self) -> str:
        """Pick a model: MEP_MODEL, else the loaded local model, else the provider default.

        For the local provider this deliberately prefers whatever LM Studio currently has loaded
        over the hard-coded default — the user swapping models in LM Studio should just work,
        without editing config here.
        """
        explicit = os.getenv("MEP_MODEL")
        if explicit:
            return explicit

        default = PROVIDERS[self.provider]["model"]
        if self.provider != "local":
            return default

        try:
            available = self.list_models()
        except (APIError, APIConnectionError):
            return default
        if not available:
            return default
        # LM Studio lists the loaded model first; skip embedding models, which can't chat.
        for mid in available:
            if "embed" not in mid.lower():
                return mid
        return default

    # -- completion --------------------------------------------------------

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        schema: dict | None = None,
        schema_name: str = "response",
        prompt_version: str = "unversioned",
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> Completion:
        """One completion. With `schema`, constrains output to that JSON Schema and parses it.

        temperature defaults to 0: extraction should be as close to reproducible as the stack
        allows. Raise it only for tasks where variation is the point — there aren't any here.
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": True},
            }

        started = time.monotonic()
        response = self._call_with_retry(**kwargs)
        elapsed = time.monotonic() - started

        choice = response.choices[0]
        raw = choice.message.content or ""
        text = strip_think(raw)

        usage = getattr(response, "usage", None)
        details = getattr(usage, "completion_tokens_details", None)
        result = Completion(
            text=text,
            raw_text=raw,
            provider=self.provider,
            model=response.model or self.model,
            prompt_version=prompt_version,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            reasoning_tokens=getattr(details, "reasoning_tokens", 0) or 0,
            finish_reason=choice.finish_reason or "",
            elapsed_s=round(elapsed, 2),
        )

        # `finish_reason == "length"` alone does NOT mean the payload is unusable. A model can
        # emit a complete, valid JSON object and then keep generating until it hits the cap — the
        # object is fine and rejecting it would throw away good work. So: try to parse, and treat
        # the truncation as fatal only when there is nothing usable to parse.
        hit_ceiling = result.finish_reason == "length"

        if schema is not None:
            try:
                result.parsed = self._parse_json(text)
            except ValueError as exc:
                if hit_ceiling:
                    raise self._truncation_error(result, max_tokens) from exc
                raise
            if hit_ceiling:
                print(
                    f"  [llm] note: hit the {max_tokens}-token ceiling but the JSON parsed "
                    f"cleanly ({result.reasoning_tokens} reasoning tokens). Output is usable; "
                    f"raise max_tokens if this recurs.",
                    file=sys.stderr,
                )
        elif hit_ceiling and not text.strip():
            raise self._truncation_error(result, max_tokens)

        return result

    @staticmethod
    def _truncation_error(result: Completion, max_tokens: int) -> TruncatedResponse:
        thinking = (
            f", {result.reasoning_tokens} of them on reasoning" if result.reasoning_tokens else ""
        )
        return TruncatedResponse(
            f"hit the {max_tokens}-token ceiling{thinking} and returned "
            f"{len(result.text)} chars of unparseable content. Raise max_tokens — on a reasoning "
            f"model the budget covers thinking *and* output, so it must exceed both."
        )

    @staticmethod
    def _parse_json(text: str) -> Any:
        """Parse JSON, tolerating a model that wrapped it in prose or a code fence.

        Constrained decoding should make this unnecessary, but support varies by backend and a
        salvageable response beats a hard failure mid-batch. A genuine parse failure still raises —
        silently returning None would let bad extractions into the catalog.
        """
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if fenced:
            try:
                return json.loads(fenced.group(1))
            except json.JSONDecodeError:
                pass

        start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
        if start != -1:
            for end in range(len(text), start, -1):
                try:
                    return json.loads(text[start:end])
                except json.JSONDecodeError:
                    continue

        raise ValueError(f"could not parse JSON from response: {text[:400]!r}")

    def _call_with_retry(self, **kwargs):
        last = None
        for attempt in range(MAX_RETRIES):
            try:
                return self._client.chat.completions.create(**kwargs)
            except (APIError, APIConnectionError) as exc:
                last = exc
                msg = str(exc).lower()
                fatal = any(
                    k in msg for k in ("invalid api key", "unauthorized", "insufficient credit")
                )
                if fatal or attempt == MAX_RETRIES - 1:
                    break
                delay = RETRY_BASE_DELAY * (2**attempt)
                print(
                    f"  [llm] {type(exc).__name__} (attempt {attempt + 1}/{MAX_RETRIES}), "
                    f"retrying in {delay:.0f}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
        raise RuntimeError(f"LLM call failed after {MAX_RETRIES} attempts: {last}") from last


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--provider", default=None, help="local | openrouter")
    ap.add_argument("--model", default=None)
    ap.add_argument("--check", action="store_true", help="probe the endpoint and list models")
    ap.add_argument("--prompt", default=None, help="one-shot prompt")
    args = ap.parse_args()

    kwargs = {}
    if args.provider:
        kwargs["provider"] = args.provider
    if args.model:
        kwargs["model"] = args.model
    client = LLMClient(**kwargs)

    print(f"provider : {client.provider}")
    print(f"base_url : {client.base_url}")
    print(f"model    : {client.model}")

    if args.check:
        try:
            models = client.list_models()
        except Exception as exc:
            print(f"\nendpoint unreachable: {exc}")
            return 1
        print(f"\n{len(models)} model(s) available:")
        for m in models:
            print(f"  {'*' if m == client.model else ' '} {m}")

    if args.prompt:
        result = client.complete(args.prompt, max_tokens=512)
        print(f"\n{result.text}")
        print(
            f"\n[{result.completion_tokens} tok out, {result.elapsed_s}s, "
            f"provenance={result.provenance()}]"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
