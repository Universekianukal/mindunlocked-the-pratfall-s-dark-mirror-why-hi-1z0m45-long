"""Provider shim so the pipeline runs whether or not Anthropic has credits.

Every generation script here (script + beats, YouTube metadata, thumbnail copy)
goes through claude-sonnet-5 / claude-haiku. When the Anthropic balance hits
zero the whole pipeline stops dead -- and it did: every build from late July
2026 on failed, some with a clean

    anthropic.BadRequestError: ... 'Your credit balance is too low ...'

and the two most recent with a bare StopIteration, because _gen_video_content
did `next(b.text for b in resp.content ...)` with no default and an empty
response has no text block.

Fireworks is the fallback. The comic pipeline has already proven glm-5p2 on
the same kind of scriptwriting work.

SWITCHING BACK once Anthropic is topped up -- pick either:
  * set the repo/org Actions variable  LLM_PROVIDER = anthropic   (no commit), or
  * unset LLM_PROVIDER and remove/blank FIREWORKS_API_KEY; "auto" then falls
    back to Anthropic whenever its key is present.
Nothing else changes -- the per-script MODEL constants (sonnet / haiku) are
still what the Anthropic path uses.

WHY A SHIM, NOT A REWRITE. Every caller consumes the response the same way
(`next(b.text for b in resp.content if b.type == "text")`). This returns an
object with that same surface, so each call site changes by one line:

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])  ->  client = _llm.client()

NOT FOR VISION. The thumbnail QA pass sends a base64 image; glm-5p2 is
text-only. Image content blocks raise here rather than being silently dropped
-- _gen_thumbnail already skips the QA call entirely when the provider isn't
Anthropic (a dropped image would otherwise turn "QA" into a rubber stamp).
"""
import json
import os
import time
import urllib.error
import urllib.request

FEATHERLESS_URL = "https://api.featherless.ai/v1/chat/completions"
# Non-reasoning, 131k ctx: returns the JSON straight away instead of spending
# its token budget thinking (glm-5p2's failure mode). Concurrency cost 4 --
# on a smaller Featherless plan set FEATHERLESS_MODEL=openai/gpt-oss-120b.
FEATHERLESS_MODEL = os.environ.get(
    "FEATHERLESS_MODEL", "deepseek-ai/DeepSeek-V3.2"
)
FIREWORKS_URL = "https://api.fireworks.ai/inference/v1/chat/completions"
FIREWORKS_MODEL = os.environ.get(
    "FIREWORKS_MODEL", "accounts/fireworks/models/glm-5p2"
)

# Reasoning models think past any budget they're given: on a comparable script
# prompt glm-5p2 produced ~100k characters of reasoning and still returned no
# content at a 32k-token cap. Capping the effort is what makes the call
# terminate with actual output.
REASONING_EFFORT = os.environ.get("FIREWORKS_REASONING_EFFORT", "low")


class TextBlock:
    """Mimics an Anthropic content block: `.type` and `.text`."""

    type = "text"

    def __init__(self, text):
        self.text = text


class Response:
    """Mimics an Anthropic message: `.content` (blocks) and `.stop_reason`."""

    def __init__(self, text, stop_reason):
        self.content = [TextBlock(text)]
        self.stop_reason = stop_reason


def _flatten(messages):
    """Anthropic messages -> OpenAI chat messages.

    Anthropic allows content to be a list of typed blocks; chat-completions
    wants a plain string. Text blocks are joined; anything else is refused
    loudly (see the module docstring on why silence would be worse).
    """
    out = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            kinds = {b.get("type") for b in content if isinstance(b, dict)}
            if kinds - {"text"}:
                raise ValueError(
                    "_llm shim received non-text content blocks "
                    f"({sorted(kinds - {'text'})}). Fireworks glm-5p2 is "
                    "text-only -- keep vision calls on Anthropic."
                )
            content = "\n".join(
                b.get("text", "") for b in content if isinstance(b, dict)
            )
        out.append({"role": m.get("role", "user"), "content": content or ""})
    return out


class _FireworksMessages:
    # Overridden by _FeatherlessMessages: both are OpenAI-compatible.
    URL = FIREWORKS_URL
    MODEL = FIREWORKS_MODEL
    LABEL = "Fireworks"
    REASONING = True

    def __init__(self, api_key):
        self._key = api_key

    def create(self, model=None, max_tokens=4096, system=None, messages=None,
               **_ignored):
        """Same signature as Anthropic's messages.create, for the fields we use.

        `model` (the Anthropic slug) is ignored -- Fireworks uses FIREWORKS_MODEL.
        """
        msgs = ([{"role": "system", "content": system}] if system else []) + \
            _flatten(messages or [])
        payload = {
            "model": self.MODEL,
            "max_tokens": max_tokens,
            "messages": msgs,
        }
        if self.REASONING:
            payload["reasoning_effort"] = REASONING_EFFORT
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.URL, data=data,
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
                # Cloudflare answers urllib's default Python-urllib/3.x agent
                # with 1010; a non-default UA avoids that.
                "User-Agent": "mindunlocked-pipeline",
            },
        )

        last = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=1800) as r:
                    body = json.load(r)
                break
            except urllib.error.HTTPError as e:
                detail = e.read()[:500].decode("utf-8", "replace")
                # 4xx other than 429 won't improve on retry -- fail now with
                # the provider's own message instead of burning 3 attempts.
                if 400 <= e.code < 500 and e.code != 429:
                    raise RuntimeError(
                        f"{self.LABEL} HTTP {e.code}: {detail}"
                    ) from None
                last = f"HTTP {e.code}: {detail}"
            except Exception as e:  # noqa: BLE001 - network / timeout / bad body
                last = repr(e)
            if attempt < 2:
                time.sleep(2 ** attempt * 3)
        else:
            raise RuntimeError(
                f"{self.LABEL} call failed after 3 attempts: {last}"
            )

        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        finish = choice.get("finish_reason")

        # Map the finish reason onto Anthropic's vocabulary so a caller's
        # truncation retry (if any) keeps working: a reasoning model that
        # spent its budget thinking is the same recoverable condition as
        # Anthropic's max_tokens.
        stop_reason = "max_tokens" if finish == "length" else "end_turn"
        if not text.strip() and finish != "length":
            reasoning = message.get("reasoning_content") or ""
            hint = (
                f" It produced {len(reasoning)} chars of reasoning; raise "
                "max_tokens or lower FIREWORKS_REASONING_EFFORT."
                if reasoning else ""
            )
            raise RuntimeError(
                f"{self.LABEL} returned empty content (finish_reason={finish!r})."
                f"{hint}"
            )
        return Response(text, stop_reason)


class _FeatherlessMessages(_FireworksMessages):
    URL = FEATHERLESS_URL
    MODEL = FEATHERLESS_MODEL
    LABEL = "Featherless"
    REASONING = False


class _FeatherlessClient:
    """Anthropic-shaped client backed by Featherless."""

    def __init__(self, api_key):
        self.messages = _FeatherlessMessages(api_key)


class _FireworksClient:
    """Anthropic-shaped client backed by Fireworks."""

    def __init__(self, api_key):
        self.messages = _FireworksMessages(api_key)


def provider():
    """Which provider this run uses. 'auto' resolves on which key is present."""
    choice = (os.environ.get("LLM_PROVIDER") or "auto").strip().lower()
    if choice in ("anthropic", "fireworks", "featherless"):
        return choice
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("FEATHERLESS_API_KEY"):
        return "featherless"
    if os.environ.get("FIREWORKS_API_KEY"):
        return "fireworks"
    raise RuntimeError(
        "No LLM key found. Set FIREWORKS_API_KEY (or ANTHROPIC_API_KEY), or "
        "pin one with the LLM_PROVIDER variable."
    )


def client():
    """Return a client exposing .messages.create(), for whichever provider."""
    name = provider()
    if name == "anthropic":
        from anthropic import Anthropic
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set"
            )
        print("LLM provider: anthropic", flush=True)
        return Anthropic(api_key=key)

    if name == "featherless":
        key = os.environ.get("FEATHERLESS_API_KEY")
        if not key:
            raise RuntimeError(
                "LLM_PROVIDER=featherless but FEATHERLESS_API_KEY is not set"
            )
        print(f"LLM provider: featherless ({FEATHERLESS_MODEL})", flush=True)
        return _FeatherlessClient(key)

    key = os.environ.get("FIREWORKS_API_KEY")
    if not key:
        raise RuntimeError(
            "LLM_PROVIDER=fireworks but FIREWORKS_API_KEY is not set"
        )
    print(f"LLM provider: fireworks ({FIREWORKS_MODEL})", flush=True)
    return _FireworksClient(key)


def only_text(resp):
    """Text of an Anthropic-shaped response, or a clear error (never StopIteration)."""
    t = next(
        (b.text for b in getattr(resp, "content", [])
         if getattr(b, "type", None) == "text"),
        None,
    )
    if not t or not t.strip():
        raise RuntimeError(
            "LLM returned no text (stop_reason="
            f"{getattr(resp, 'stop_reason', '?')}). If Anthropic credits are "
            "exhausted, top up or set LLM_PROVIDER=fireworks."
        )
    return t
