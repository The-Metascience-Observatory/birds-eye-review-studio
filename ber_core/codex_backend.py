"""One-shot Codex CLI transport. No review files or resumable sessions are exposed."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

LUNA_MODEL = "gpt-5.6-luna"
TERRA_MODEL = "gpt-5.6-terra"


class CodexUsageLimit(RuntimeError):
    """The caller should pause its batch until account capacity is available."""


def resolve_model(model: str) -> str:
    # Existing reviews have Claude tier names. Selecting Codex opts these into
    # Luna without rewriting the review's configuration for other providers.
    if not model or model in {"luna", "haiku", "sonnet", "opus"}:
        return LUNA_MODEL
    if model == "terra":
        return TERRA_MODEL
    return model


def find_binary() -> str | None:
    return shutil.which("codex") or (
        str(Path.home() / ".local/bin/codex")
        if os.access(Path.home() / ".local/bin/codex", os.X_OK) else None
    )


def call_codex(prompt, model, identifier, timeout=120, resume_session=None,
               use_stdin=False, read_files=None, output_schema=None, image_paths=None,
               stop_on_usage_limit=False):
    # Imported lazily: claude_cli owns the backwards-compatible response type
    # and caller-scoped credentials; it imports this transport for registration.
    from ber_core.claude_cli import (
        ClaudeResponse, _child_env, _latch_auth_failure,
        _AUTH_FAILURE_PHRASES, run_halted,
    )

    if run_halted():
        return ClaudeResponse()

    if resume_session:
        raise ValueError("Codex calls are stateless; retry with the full prompt")
    if read_files:
        # The caller can retain its text extraction. Never ask a text-only
        # transport to hallucinate the contents of an inaccessible PDF.
        print(f"Codex: direct PDF reading unavailable for {identifier}; retaining text extraction",
              file=sys.stderr, flush=True)
        return ClaudeResponse()
    binary = find_binary()
    if not binary:
        raise RuntimeError("Codex CLI not found; install it and run `codex login` on this host")

    env = _child_env()
    effort = env.get("BER_CODEX_REASONING_EFFORT", "medium").strip().lower()
    if effort not in {"low", "medium", "high"}:
        raise ValueError("BER_CODEX_REASONING_EFFORT must be low, medium, or high")
    # This provider explicitly uses the host's Codex login, not inherited API
    # credentials from an operator's .env or another provider's scoped context.
    from ber_core.environment import MANAGED_VARS
    for key in MANAGED_VARS:
        env.pop(key, None)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="ber-codex-") as work:
        output = Path(work) / "answer.txt"
        cmd = [binary, "exec", "--model", resolve_model(model),
               "--ephemeral", "--ignore-user-config", "--ignore-rules",
               "--skip-git-repo-check", "--sandbox", "read-only",
               "--json", "--color", "never", "--output-last-message", str(output),
               "-c", 'approval_policy="never"',
               "-c", 'web_search="disabled"',
               "-c", "features.shell_tool=false",
               "-c", "features.apps=false", "-c", "features.plugins=false",
               "-c", "features.multi_agent=false",
               "-c", "features.skip_host_skill_discovery=true",
               "-c", f"model_reasoning_effort={json.dumps(effort)}", "-"]
        if output_schema is not None:
            schema_path = Path(work) / "schema.json"
            schema_path.write_text(json.dumps(output_schema), encoding="utf-8")
            cmd[-1:-1] = ["--output-schema", str(schema_path)]
        for path in image_paths or ():
            cmd[-1:-1] = ["--image", str(Path(path).resolve())]
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                  cwd=work, env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"Codex timeout for {identifier} after {timeout}s", file=sys.stderr, flush=True)
            return ClaudeResponse(elapsed=time.monotonic() - started)
        except OSError as exc:
            raise RuntimeError(f"Could not start Codex CLI: {exc}") from exc

        response = ClaudeResponse(elapsed=time.monotonic() - started)
        completed = False
        failed = False
        diagnostics = [proc.stderr or ""]
        for line in proc.stdout.splitlines():
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "turn.failed":
                failed = True
                diagnostics.append(json.dumps(event.get("error") or ""))
            elif event.get("type") == "error":
                diagnostics.append(json.dumps(event))
            if event.get("type") == "turn.completed":
                completed = True
                usage = event.get("usage") or {}
                if isinstance(usage, dict):
                    for field in ("input_tokens", "output_tokens"):
                        value = usage.get(field)
                        if isinstance(value, int) and value >= 0:
                            setattr(response, field, getattr(response, field) + value)
        if proc.returncode or failed or not completed:
            diagnostic = "\n".join(diagnostics).lower()
            auth_phrases = (*_AUTH_FAILURE_PHRASES, "401 unauthorized", "status code: 401",
                            "token_expired", "expired_token", "refresh_token_reused",
                            "token has expired", "not logged in")
            if any(phrase in diagnostic for phrase in auth_phrases):
                _latch_auth_failure(identifier,
                    "Codex authentication was rejected; sign in again with `codex login`.")
                return response
            if any(phrase in diagnostic for phrase in (
                    "usage_limit_reached", "usage limit", "weekly limit", "you've hit your limit")):
                reason = "Codex account usage limit reached; completed results are saved. Resume after the limit resets."
                if stop_on_usage_limit:
                    raise CodexUsageLimit(reason)
                from ber_core.usage_guard import request_stop
                request_stop(reason)
                print(reason, file=sys.stderr, flush=True)
                return response
            # Do not accept a partial answer, leak raw subprocess diagnostics
            # (which can contain credentials), or fall back to another provider.
            print(f"Codex failed for {identifier} (exit={proc.returncode}, "
                  f"completed={completed}); check `codex login status` and model access",
                  file=sys.stderr, flush=True)
            return response
        if output.exists():
            response.text = output.read_text(encoding="utf-8").strip()
        # No session_id: caller retries must replay the complete paper/prompt.
        # Codex reports tokens but not USD billing; cost_usd=0 means unreported.
        return response
