#!/usr/bin/env python3
"""Run lint, the isolated pipeline/server suites and frontend type checks from one command."""
import argparse
import os
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pipeline-python', default=sys.executable)
    parser.add_argument('--server-python', default=str(root / 'server/.venv/bin/python'))
    parser.add_argument('--skip-frontend', action='store_true')
    args = parser.parse_args()
    # Plugin autoload is off (a broken third-party plugin in a user site can
    # break collection), so the plugins the suites need are named explicitly.
    # The configured addopts stay in force: both suites run in parallel
    # (-n auto --dist loadfile) and report skips (-rs).
    env = {**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
    lint_paths = ['run_pipeline.py', 'steps', 'ber_core', 'scripts', 'tests',
                  'server/app', 'server/tests', 'server/scripts']
    checks = [
        (root, [args.pipeline_python, '-m', 'pyflakes', *lint_paths]),
        (root, [args.pipeline_python, '-m', 'pytest', '-p', 'xdist', '-q']),
        (root / 'server', [args.server_python, '-m', 'pytest', '-p', 'xdist',
                           '-p', 'pytest_asyncio.plugin', '-q']),
    ]
    if not args.skip_frontend:
        checks.append((root, ['pnpm', '-r', 'typecheck']))
    for cwd, command in checks:
        print(f'Checking {cwd.name}: {" ".join(command)}', flush=True)
        result = subprocess.run(command, cwd=cwd, env=env)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
