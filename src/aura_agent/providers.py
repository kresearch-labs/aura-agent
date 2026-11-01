"""providers.py — the cloud providers setup can connect from one pasted key.

Key prefixes are only the ones two sources agree on (provider docs /
secret-scanner rules); an unknown key is never guessed — the user picks the
provider.
"""
from __future__ import annotations

CLOUD_PROVIDERS: dict[str, dict] = {
    "openai": dict(
        label="OpenAI (GPT)",
        base_url="https://api.openai.com/v1",
        key_env="OPENAI_API_KEY",
        key_page="platform.openai.com/api-keys",
        key_prefixes=("sk-proj-", "sk-svcacct-"),
        note="works with an API key, through OpenAI's Responses API — every "
             "model answers, and its reasoning carries from step to step.",
    ),
    "anthropic": dict(
        label="Anthropic (Claude)",
        base_url="https://api.anthropic.com/v1",
        key_env="ANTHROPIC_API_KEY",
        key_page="console.anthropic.com",
        key_prefixes=("sk-ant-api",),
        # Anthropic's own model list takes the key in x-api-key
        list_headers={"x-api-key": "{key}", "anthropic-version": "2023-06-01"},
        list_query="?limit=1000",
        note="works with an API key, through Anthropic's own Messages API — "
             "prompt caching, and thinking shown and carried between steps.",
    ),
    "google": dict(
        label="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_env="GEMINI_API_KEY",
        key_page="aistudio.google.com/apikey",
        key_prefixes=("AIza",),
        note="works with an API key, through Google's OpenAI-compatible "
             "address (beta).",
    ),
    "openrouter": dict(
        label="OpenRouter (hundreds of models, one key)",
        base_url="https://openrouter.ai/api/v1",
        key_env="OPENROUTER_API_KEY",
        key_page="openrouter.ai/keys",
        key_prefixes=("sk-or-",),
        note="one key reaches models from many companies; prices differ per model.",
    ),
    "deepseek": dict(
        label="DeepSeek",
        base_url="https://api.deepseek.com",
        key_env="DEEPSEEK_API_KEY",
        key_page="platform.deepseek.com",
        note="works with an API key.",
    ),
    "xai": dict(
        label="xAI (Grok)",
        base_url="https://api.x.ai/v1",
        key_env="XAI_API_KEY",
        key_page="console.x.ai",
        key_prefixes=("xai-",),
        note="works with an API key.",
    ),
    "mistral": dict(
        label="Mistral",
        base_url="https://api.mistral.ai/v1",
        key_env="MISTRAL_API_KEY",
        key_page="console.mistral.ai",
        note="works with an API key.",
    ),
    "groq": dict(
        label="Groq (fast open models)",
        base_url="https://api.groq.com/openai/v1",
        key_env="GROQ_API_KEY",
        key_page="console.groq.com/keys",
        key_prefixes=("gsk_",),
        note="works with an API key; has a free tier with low limits.",
    ),
    "together": dict(
        label="Together AI (open models)",
        base_url="https://api.together.xyz/v1",
        key_env="TOGETHER_API_KEY",
        key_page="api.together.ai",
        key_prefixes=("tgp_v1_",),
        note="works with an API key.",
    ),
    "fireworks": dict(
        label="Fireworks AI (open models)",
        base_url="https://api.fireworks.ai/inference/v1",
        key_env="FIREWORKS_API_KEY",
        key_page="fireworks.ai",
        note="works with an API key.",
    ),
    "cerebras": dict(
        label="Cerebras (fast open models)",
        base_url="https://api.cerebras.ai/v1",
        key_env="CEREBRAS_API_KEY",
        key_page="cloud.cerebras.ai",
        note="works with an API key.",
    ),
    "openai-compatible": dict(
        label="Any other OpenAI-compatible address",
        base_url="",          # The user types it
        key_env="",
        key_page="",
        note="any server that speaks the OpenAI chat format.",
    ),
}

# keys that look right but can't run a model — say why instead of a 401
_NOT_FOR_MODELS = (
    ("sk-ant-oat", "this is a Claude subscription sign-in token. Anthropic doesn't "
                   "allow other apps to use Pro/Max subscriptions — make an API key "
                   "at console.anthropic.com"),
    ("sk-ant-admin", "this is an Anthropic admin key — it manages the account but "
                     "can't run models. Make an API key at console.anthropic.com"),
    ("sk-admin-", "this is an OpenAI admin key — it manages the account but can't "
                  "run models. Make a project key at platform.openai.com/api-keys"),
)


def unusable_key(key: str) -> str | None:
    """Why this key can't run a model (it's a different kind of key), or None."""
    for prefix, why in _NOT_FOR_MODELS:
        if key.startswith(prefix):
            return why
    return None


def detect_provider_from_key(key: str) -> str | None:
    """The provider whose documented key prefix this key has — longest
    prefix first; None when unsure (a plain 'sk-' key is OpenAI's old form
    AND DeepSeek's, so the user is asked)."""
    hits = [(len(p), name) for name, d in CLOUD_PROVIDERS.items()
            for p in d.get("key_prefixes") or () if key.startswith(p)]
    return max(hits)[1] if hits else None


def provider_for_url(base_url: str) -> str | None:
    """Which listed provider an address belongs to (by host)."""
    from urllib.parse import urlsplit
    host = (urlsplit(base_url or "").hostname or "").lower()
    for name, d in CLOUD_PROVIDERS.items():
        if d["base_url"] and (urlsplit(d["base_url"]).hostname or "") == host:
            return name
    return None
