"""Anthropic client wrapper: structured outputs, usage accounting, refusals.

Every call goes through `parse()`, which constrains the response to a Pydantic
model. That is what enforces the controlled vocabularies -- an off-taxonomy
value fails at the API boundary rather than turning up later as a one-off
category nobody notices until the analysis looks wrong.
"""

import base64
import threading
from dataclasses import dataclass, field

import anthropic

DEFAULT_MODEL = "claude-opus-5"

# Effort is set per pass. The gate is classification and localisation, which is
# not intelligence-sensitive; the extraction pass writes an HTML reproduction,
# which is. Paying `high` on both would roughly double spend for no gain on the
# cheap half.
EFFORT = {"gate": "low", "extract": "high", "derive": "low"}
MAX_TOKENS = {"gate": 4000, "extract": 12000, "derive": 2000}


# Adaptive thinking and output_config.effort belong to the 4.6-and-later
# generation. Older models reject them outright -- `effort` errors on Sonnet 4.5
# and Haiku 4.5, and those models take thinking as {type: "enabled",
# budget_tokens: N} rather than adaptive. Sending them unconditionally makes
# every request fail before it does any work, which is exactly what happened
# when the gate was first pointed at Haiku to save money.
#
# Anything not listed here gets the conservative request shape: no thinking
# parameter and no effort, which is valid on every model. That costs a little
# quality on an unrecognised model and costs nothing on a recognised one, which
# is the right way round for a default.
_ADAPTIVE_MODELS = (
    "claude-opus-5", "claude-fable-5", "claude-mythos-5", "claude-sonnet-5",
    "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6",
)


def supports_adaptive_thinking(model):
    """Whether `model` accepts adaptive thinking and an effort setting."""
    return any(model.startswith(prefix) for prefix in _ADAPTIVE_MODELS)


class ConfigurationError(Exception):
    """The request is wrong, not the data.

    A bad model id, an unsupported parameter, or a missing key fails the same
    way on every image. Treating it as a per-row outcome means burning through
    the whole corpus to collect thousands of identical failures, so this
    aborts the run instead.
    """


class RefusalError(Exception):
    """The model declined. Distinct from a transport failure: retrying the
    same request will decline again, so it is a row outcome, not an error to
    back off on."""


@dataclass
class Usage:
    """Running token totals, so a pilot reports real cost rather than an
    estimate carried over from a spec."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, usage):
        with self._lock:
            self.calls += 1
            self.input_tokens += getattr(usage, "input_tokens", 0) or 0
            self.output_tokens += getattr(usage, "output_tokens", 0) or 0
            self.cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0

    def as_dict(self):
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read,
        }


def image_block(path):
    """Base64 image content block. JPEG is what the fetch stage writes."""
    with open(path, "rb") as fh:
        data = base64.standard_b64encode(fh.read()).decode("ascii")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": data},
    }


def _parsed(message):
    """Pull the validated model out of a ParsedMessage."""
    for block in message.content:
        if block.type == "text" and getattr(block, "parsed_output", None) is not None:
            return block.parsed_output
    raise ValueError("response contained no parsed output")


class Reader:
    def __init__(self, model=DEFAULT_MODEL, gate_model=None, max_retries=3, timeout=180.0):
        self._client = anthropic.Anthropic(max_retries=max_retries, timeout=timeout)
        self.model = model
        # The gate may run a cheaper model than extraction, but that is a
        # decision for pilot crop-accuracy numbers -- a bad bounding box
        # poisons everything downstream of it, so it defaults to the same
        # model rather than quietly economising.
        self.gate_model = gate_model or model
        self.usage = Usage()

    def _call(self, pass_name, model, system, content, output_format):
        request = {
            "model": model,
            "max_tokens": MAX_TOKENS[pass_name],
            # The system block is byte-identical across every image, so it is
            # the natural cache prefix. Volatile content (the image) follows
            # it, never precedes it.
            "system": [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }],
            "messages": [{"role": "user", "content": content}],
            "output_format": output_format,
        }
        if supports_adaptive_thinking(model):
            request["thinking"] = {"type": "adaptive"}
            request["output_config"] = {"effort": EFFORT[pass_name]}

        try:
            message = self._client.messages.parse(**request)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                anthropic.NotFoundError, anthropic.BadRequestError) as exc:
            # Surface the API's own words. The failure this replaces reported
            # "3 frame(s) unusable" for what was really a rejected request,
            # which is a symptom with the cause deleted.
            raise ConfigurationError(
                "%s rejected the %s request for model %r: %s"
                % (type(exc).__name__, pass_name, model, exc)
            ) from exc

        self.usage.add(message.usage)
        if message.stop_reason == "refusal":
            detail = getattr(message.stop_details, "category", None)
            raise RefusalError("model declined (%s)" % (detail or "unspecified"))
        return _parsed(message)

    def gate(self, image_path, user_text, output_format):
        return self._call(
            "gate", self.gate_model, self._gate_system,
            [image_block(image_path), {"type": "text", "text": user_text}],
            output_format,
        )

    def extract(self, image_path, user_text, output_format):
        return self._call(
            "extract", self.model, self._extract_system,
            [image_block(image_path), {"type": "text", "text": user_text}],
            output_format,
        )

    def derive(self, user_text, output_format):
        return self._call(
            "derive", self.model, self._derive_system,
            [{"type": "text", "text": user_text}],
            output_format,
        )

    # System prompts are attached by the caller so prompts.py stays the single
    # place any prompt text lives.
    _gate_system = ""
    _extract_system = ""
    _derive_system = ""

    def with_prompts(self, gate, extract, derive):
        self._gate_system, self._extract_system, self._derive_system = gate, extract, derive
        return self
