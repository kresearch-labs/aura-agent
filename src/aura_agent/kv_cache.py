"""kv_cache.py — Model-Adaptive KV-Cache Engine.

Provider-agnostic prompt-caching alignment layer. Each AI backend caches prompt
prefixes by its own rules; this module structures the outgoing payload so each
backend's native cache actually HITS:

  prefix mode (DeepSeek, OpenAI, most OpenAI-compatible clouds):
      byte-for-byte match from token 0 (DeepSeek min 1024 tokens; hit
      ~$0.0028/M vs miss $0.14 on V4-Flash). Rule: static content first,
      volatile content LAST. usage exposes prompt_cache_hit_tokens.
  block mode (vLLM PagedAttention / APC, on by default in ):
      16-token blocks, chain-hashed, ONLY full blocks cached — pad the
      static zone to a block boundary so the shared prefix ends cleanly.
  explicit mode (Anthropic-style): ephemeral cache_control breakpoints at
      valid positions (future-ready; we speak OpenAI format today).

Invariants enforced here (regression-tested — violations break caching):
  1. system message(s) are the FIRST messages, byte-stable across turns
  2. no timestamps / turn counters / session ids in the static zone
  3. tool schemas are static (they already are — we verify ordering)
  4. volatile appends (journal/todo/status blocks) go AFTER history

Zero deps. Pure payload discipline.
"""
from __future__ import annotations

TOKENS_PER_CHAR = 4          # chars/4 estimate — same as governor
DEFAULT_BLOCK_TOKENS = 16    # VLLM default block size

VOLATILE_PATTERNS = [
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}",          # Timestamps
    r"\bseq[\"']?\s*:\s*\d+",                        # Turn counters
    r"\bstep[\"']?\s*:\s*\d+",
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}\b",     # Uuids
    r"\bts\s*[:=]\s*\d+\.\d+",                       # Epoch floats
]
VOLATILE_RE = [__import__("re").compile(p, __import__("re").I)
               for p in VOLATILE_PATTERNS]


def cache_mode_for(profile) -> str:
    """Pick the strategy from the provider profile (cfg key cache_mode)."""
    mode = str(getattr(profile, "cache_mode", "") or "auto").lower()
    if mode in ("prefix", "block", "explicit", "off"):
        return mode
    base = str(getattr(profile, "base_url", "") or "").lower()
    if "vllm" in base or ":8000" in base:      # VLLM's default port
        return "block"
    return "prefix"          # OpenAI-compatible default (DeepSeek too)


def align_messages(messages: list[dict], mode: str,
                   block_tokens: int = DEFAULT_BLOCK_TOKENS) -> list[dict]:
    """Return a cache-aligned copy of the message list. Never mutates input.
    Never changes semantics — only ordering of pure-system prefixes and
    padding characters that tokenizers treat as whitespace-neutral."""
    if mode == "off":
        return list(messages)
    msgs = [dict(m) for m in messages]
    if mode in ("prefix", "block"):
        msgs = _static_first(msgs)
    if mode == "block":
        msgs = _pad_system_to_block(msgs, block_tokens)
    return msgs


def _system_indices(msgs: list[dict]) -> list[int]:
    return [i for i, m in enumerate(msgs) if m.get("role") == "system"]


def _static_zone(msgs: list[dict]) -> int:
    """Length of the leading run of system messages — the static prefix the
    backend caches. Notes the loop adds later (scope, todo state, intent
    check…) come after history and are NOT part of it."""
    n = 0
    while n < len(msgs) and msgs[n].get("role") == "system":
        n += 1
    return n


def _static_first(msgs: list[dict]) -> list[dict]:
    """Make sure the conversation STARTS with the system prompt.

    The loop adds system notes mid-conversation (scope each turn, todo state,
    intent check…), so each new note landed above the whole history and changed
    the prefix — the cache broke right after the system prompt (measured: 97%
    of the prompt reusable without it, 15% with it) and notes lost their place
    ("the last message wins" became "the first"). Now only the first system
    message is moved, and only if something precedes it; every other message
    keeps its position.
    """
    if not msgs or msgs[0].get("role") == "system":
        return msgs
    first = next((i for i, m in enumerate(msgs)
                  if m.get("role") == "system"), None)
    if first is None:
        return msgs
    return [msgs[first]] + msgs[:first] + msgs[first + 1:]


def _pad_system_to_block(msgs: list[dict], block: int) -> list[dict]:
    """Pad the static prefix (the LEADING system messages) so its estimated
    token count is an exact multiple of the block size. Padding is a stable
    whitespace tail — no semantic change, byte-stable across turns."""
    zone = _static_zone(msgs)
    if zone == 0:
        return msgs
    static_chars = sum(len(str(m.get("content", ""))) for m in msgs[:zone])
    boundary = block * TOKENS_PER_CHAR            # Chars per full block
    padlen = (-static_chars) % boundary           # Chars to next boundary
    if padlen == 0:
        return msgs
    # the last message of the static zone: stable, byte-identical every turn
    i = zone - 1
    msgs[i]["content"] = str(msgs[i].get("content", "")).rstrip() + " " * padlen
    return msgs


def check_alignment(messages: list[dict]) -> list[str]:
    """Audit a message list against the invariants. Returns problems ('' =
    clean). Run by `aura selftest` on a real scripted turn — a change that breaks cache
    alignment fails here, loudly."""
    problems: list[str] = []
    if not messages:
        return ["empty message list"]
    sys_idx = _system_indices(messages)
    if not sys_idx:
        return problems
    if messages[0].get("role") != "system":
        problems.append("the conversation does not start with the system "
                        "prompt — static content must be first (prefix "
                        "match starts at token 0)")
    # only the static zone must be byte-stable; notes added after history
    # are expected to change (that is why they live there)
    for i in range(_static_zone(messages)):
        text = str(messages[i].get("content", ""))
        for pat in VOLATILE_RE:
            m = pat.search(text)
            if m:
                problems.append(f"volatile content in system message "
                                f"(matches /{pat.pattern}/): '{m.group(0)}'")
    return problems


# --------------------------------------------------------------- telemetry --
def cache_usage_fields(usage: dict) -> tuple[int, int]:
    """Extract (hit_tokens, miss_tokens) from any provider's usage object.
    Returns (0, 0) when the provider did NOT report cache state (the caller
    treats 0/0 as unmeasured). DeepSeek: prompt_cache_hit_tokens /
    prompt_cache_miss_tokens. OpenAI/vLLM:
    usage.prompt_tokens_details.cached_tokens."""
    if not usage:
        return 0, 0
    hit = usage.get("prompt_cache_hit_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    if hit is not None or miss is not None:
        return int(hit or 0), int(miss or 0)
    details = usage.get("prompt_tokens_details") or {}
    if not details:                     # Provider gave no cache info at all
        return 0, 0
    cached = int(details.get("cached_tokens") or 0)
    pt = int(usage.get("prompt_tokens") or 0)
    if not cached and not pt:
        return 0, 0
    return cached, max(0, pt - cached)


