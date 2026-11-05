# AGENTS.md

Instructions for coding agents (Codex, Claude Code, and others) live in
[`CLAUDE.md`](CLAUDE.md) — read it before editing. It covers the pipeline/studio
architecture, the paper store, LLM credential rules, and how to run the tests.

Notes that apply to every agent:

- The default LLM backend is the Codex CLI (`BER_LLM_PROVIDER=codex_cli`); the
  Claude CLI, a Claude OAuth token, an Anthropic API key and OpenRouter are the
  other providers. Provider names in CLAUDE.md (`claude_cli`, `claude_token`, …)
  are literal identifiers, not instructions about which agent you are.
- Run both test suites before claiming a change is done (see CLAUDE.md,
  "Running and testing").
