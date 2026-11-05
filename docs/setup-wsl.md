# Setup on Windows (WSL2)

A tested path for a fresh **WSL2 Ubuntu** install. Everything runs inside Linux;
nothing is installed on the Windows side except WSL itself.

> **Keep the repository on the Linux filesystem** (e.g. `~/birds_eye_review`),
> not under `/mnt/c/...`. The paper store uses symlinks, which fail on the
> Windows-mounted drive, and every file operation crosses the WSL↔Windows bridge:
> `pnpm install` takes tens of minutes instead of about two, and the first
> `next dev` compile takes minutes instead of seconds.

The project has three parts, each usable on its own: the **pipeline** (the
Python step scripts in `steps/`, runnable from the CLI), the **server** (`server/`, FastAPI) and
the **studio** (`studio/`, Next.js). You need the last two only for the web UI.

## Memory

PDF conversion is the memory-hungry step: each conversion worker budgets about
8 GB (docling/torch layout models). WSL2 caps its VM at about half of host RAM by
default.

The studio sizes the work to the memory that is free when each job starts
(`server/app/services/capability.py`), and `dev.sh up` prints what it decided:

```
  machine  10 cores, 4.0GB free of 8.0GB -> convert=1 extraction=2 (low-memory: fast conversion, no marker fallback)
```

Set `BER_CONVERT_WORKERS` / `BER_EXTRACTION_WORKERS` to pin it. To give WSL more
memory, create `%UserProfile%\.wslconfig` on the Windows side:

```ini
[wsl2]
memory=12GB
swap=4GB
```

then run `wsl --shutdown` in PowerShell and start the distro again.

## 1. System prerequisites

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip redis-server redis-tools \
                    iproute2 curl git build-essential
python3 --version     # needs 3.11 or newer
```

`iproute2` (`ss`), `redis-tools` (`redis-cli`) and `curl` are used by `dev.sh`.
`dev.sh up --install-deps` can install the system packages it needs itself
(`sudo apt`, and Node via nvm); without that flag it only reports what is missing.

**Node 22 + pnpm** (studio only). The repo pins pnpm through `packageManager`:

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
# restart the shell, then:
nvm install 22 && nvm use 22      # matches .nvmrc
corepack enable pnpm
```

**Redis** (server only). WSL without systemd uses `service`, not `systemctl`:

```bash
sudo service redis-server start
redis-cli ping        # -> PONG
```

`dev.sh up` detects systemd and starts Redis the right way; Redis does not
survive `wsl --shutdown`, so `dev.sh up` starts it again after one.

## 2. The pipeline (CLI)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
cp .env.example .env.local        # set EMAIL at least
```

For full-text work also install the `download` / `conversion` extras and the
sibling `fetchpdf` / `pdf4llm` packages (see the
[README](../README.md#install)). The default conversion uses Docling only; the
optional `--profile bibliographic` route also needs GROBID (Docker).
Everything up to and including the abstract screen runs without them.

**An LLM CLI, installed inside WSL.** The default provider is the Codex CLI
(`codex login`); `BER_LLM_PROVIDER=claude_cli` uses Claude Code instead
(`curl -fsSL https://claude.ai/install.sh | bash`, then `claude auth login`).
A CLI installed on *Windows* (`claude.exe`, visible through `/mnt/c/...`) cannot
run from Linux; the studio explicitly rejects such paths. The Claude installer
puts the binary in `~/.local/bin` without adding it to `PATH`; the studio looks
there anyway, and `BER_CLAUDE_BIN` overrides the path. If the login browser does
not open from WSL, copy the printed URL into a Windows browser and paste the code
back.

Alternatively use a bring-your-own provider and skip the host CLI:
`BER_LLM_PROVIDER=openrouter` with `OPENROUTER_API_KEY`.

```bash
python run_pipeline.py --config configs/config_my_topic.yaml --dry-run
python run_pipeline.py --config configs/config_my_topic.yaml
```

## 3. The server and studio

```bash
bash dev.sh up        # creates server/.venv, runs pnpm install, starts everything
bash dev.sh status
bash dev.sh down
```

Open <http://localhost:3002> (API on `:8000`; override with `API_PORT` /
`STUDIO_PORT`). WSL2 forwards `localhost` from Windows into the VM, so always use
`localhost`, not an IP address. Everything binds `127.0.0.1` unless you pass
`--lan`.

**Two interpreters.** The server runs in `server/.venv`, but it runs pipeline
*scripts* as subprocesses with the base interpreter the venv was made from — not
the venv. Either install the pipeline into that interpreter, or point the server
at the pipeline's venv in `server/.env`:

```
PIPELINE_PYTHON=/home/<you>/birds_eye_review/.venv/bin/python
```

The manual equivalent of `dev.sh up`, from the repository root (Redis running):

```bash
python3 -m venv server/.venv
server/.venv/bin/pip install -r server/requirements.txt
server/.venv/bin/uvicorn server.app.main:app --host 127.0.0.1 --port 8000
server/.venv/bin/python -m arq server.app.worker.WorkerSettings      # LLM queue
server/.venv/bin/python -m arq server.app.worker.PdfWorkerSettings   # PDF queue
pnpm install && (cd studio && npx next dev -p 3002)                  # studio
```

The SQLite dev database is created on first start. No studio environment file is
needed: the browser calls `/api` on the studio's own origin, which Next proxies to
the API.

## Sanity checks

```bash
server/.venv/bin/python -c "import fastapi, sqlalchemy, arq, requests, argon2; print('server ok')"
.venv/bin/python -c "import requests, Bio, yaml, numpy, scipy, ber_core; print('pipeline ok')"
(cd server && .venv/bin/python -m pytest -q tests/test_smoke.py)
pnpm -r typecheck
```

## WSL / Windows gotchas

| Symptom | Cause | Fix |
|---|---|---|
| Browser can't reach the studio | using a LAN or WSL IP | use `http://localhost:3002`; `dev.sh` binds loopback unless `--lan` |
| Studio loads but no data arrives | an old `studio/.env.local` pins `NEXT_PUBLIC_API_BASE` | `dev.sh up` comments it out; or delete the line |
| Run/Start does nothing, or jobs fail at once | the selected LLM CLI isn't installed **in WSL** or isn't logged in | `bash dev.sh status` shows the CLI state; install and log in inside WSL |
| Laptop freezes during PDF conversion | too little memory for the workers | raise the WSL cap (above) or set `BER_CONVERT_WORKERS=1` |
| Conversion is slow, or some PDFs get no text, on a small machine | under ~6 GB free one worker runs in fast mode without the marker fallback | expected; raise the WSL memory cap |
| `bad interpreter: python3\r` | CRLF line endings | `.gitattributes` prevents it; `sed -i 's/\r$//' <file>` |
| `System has not been booted with systemd` | WSL without systemd | `sudo service redis-server start` |
| `ensurepip is not available` | the venv module is a separate package | `sudo apt install -y python3-venv` (or the versioned package) |
| `pnpm not found` | Node/pnpm missing | install as above, or `bash dev.sh up --install-deps`, or `NODE_BIN=/path/to/bin` |
| `ENETUNREACH` / `ETIMEDOUT` in `pnpm install` | transient registry failure | re-run; pnpm resumes |
| first `next dev` compile takes minutes; symlink errors | repository under `/mnt/c` | move it under `~` |
| `ModuleNotFoundError` in a pipeline step | pipeline not installed on the interpreter the server uses | see "Two interpreters" above |
| `No module named pdf4llm` | sibling package not installed | install it from its repository, or skip conversion |
