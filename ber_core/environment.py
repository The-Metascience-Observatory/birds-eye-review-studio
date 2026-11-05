"""Standalone configuration without rehydrating a managed tenant's secrets."""
import os

MANAGED_VARS = (
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_PROFILE", "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_SERVICE_ACCOUNT_ID", "ANTHROPIC_IDENTITY_TOKEN",
    "ANTHROPIC_IDENTITY_TOKEN_FILE", "OPENROUTER_API_KEY",
    "OPENAI_API_KEY", "CODEX_API_KEY",
)
PROVIDER_KEY = {"claude_token": "CLAUDE_CODE_OAUTH_TOKEN",
                "anthropic_key": "ANTHROPIC_API_KEY", "openrouter": "OPENROUTER_API_KEY"}


def load_local_env(path, **kwargs):
    """Load standalone defaults; server children already have a complete env."""
    if os.environ.get("BER_MANAGED_RUN") == "1":
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    return load_dotenv(path, **kwargs)


def enforce_credentials(env, provider):
    """Final spawn boundary, including libraries which load their own dotenv."""
    if env.get("BER_MANAGED_RUN") == "1":
        allowed = PROVIDER_KEY.get(provider)
        for key in MANAGED_VARS:
            if key != allowed:
                env.pop(key, None)
        if allowed and not env.get(allowed):
            raise ValueError(f"Missing credential for {provider}; refusing host fallback")
    return env
