---
name: Bug report
about: Something in the pipeline or the studio doesn't work as documented
labels: bug
---

**What happened**
A clear description of the problem, and what you expected instead.

**How to reproduce**
The command or studio steps, with the relevant part of your review config
(remove anything private). Step id(s) from `run_pipeline.py --list` help.

**Output**
The error message or the relevant log lines (`bash dev.sh logs <service>` for
the studio). Please redact API keys and paths you don't want public.

**Environment**
- OS / WSL:
- Python version:
- LLM provider (`codex_cli`, `claude_cli`, …):
- Commit:
