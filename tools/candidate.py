#!/usr/bin/env python3
"""Run an isolated proxy candidate without changing the active installation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
INSTANCE = hashlib.sha256(str(ROOT).encode()).hexdigest()[:12]
STATE = Path.home() / '.bps-proxy' / 'candidates' / INSTANCE
MANIFEST = STATE / 'process.json'


def record():
    try:
        value = json.loads(MANIFEST.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def owned(info):
    pid = info.get('pid')
    if type(pid) is not int or info.get('root') != str(ROOT):
        return False
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True)
    command = result.stdout.strip()
    return result.returncode == 0 and ' -m bps_proxy ' in command and str(STATE / 'calls.json') in command


def healthy(port):
    try:
        with urlopen(f'http://127.0.0.1:{port}/health', timeout=1) as response:
            return response.status == 200 and json.load(response).get('ok') is True
    except Exception:
        return False


def start(port):
    info = record()
    if owned(info):
        if info.get('port') != port:
            raise RuntimeError('候选服务已在其他端口运行，请先停止候选服务')
        if not healthy(port):
            raise RuntimeError('候选进程存在但健康检查失败，请检查候选日志')
        return info
    if port == 8787 or not 1024 <= port <= 65535:
        raise RuntimeError('候选端口须为 1024–65535，且不能使用当前服务的 8787')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', port))
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE, 0o700)
    env = dict(os.environ)
    env['PYTHONPATH'] = str(ROOT)
    fd = os.open(STATE / 'proxy.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'ab') as log:
        process = subprocess.Popen([sys.executable, '-m', 'bps_proxy', '--port', str(port), '--state', str(STATE / 'calls.json')],
                                   cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    info = {'pid': process.pid, 'port': port, 'root': str(ROOT), 'state': str(STATE)}
    for _ in range(50):
        if process.poll() is not None:
            raise RuntimeError('候选进程启动失败，请检查 ' + str(STATE / 'proxy.log'))
        if healthy(port):
            fd = os.open(MANIFEST, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(info, stream)
            return info
        time.sleep(0.1)
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    raise RuntimeError('候选服务启动超时')


def stop():
    info = record()
    if not owned(info):
        print('候选服务未运行')
        return
    os.kill(info['pid'], signal.SIGTERM)
    for _ in range(30):
        if not owned(info):
            MANIFEST.unlink(missing_ok=True)
            print('候选服务已停止')
            return
        time.sleep(0.1)
    raise RuntimeError('候选进程尚未退出，请检查候选日志')


def cli_args(port, arguments):
    return ['codex', '-c', 'model_provider="openai"', '-c',
            f'openai_base_url="http://127.0.0.1:{port}/v1"', *arguments]


def main():
    parser = argparse.ArgumentParser(description='启动独立候选服务，不修改共享 Codex 配置')
    parser.add_argument('--port', type=int, default=18787)
    parser.add_argument('command', choices=['start', 'stop', 'status', 'run'])
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.command == 'stop':
            stop()
        elif args.command == 'status':
            info = record()
            print(json.dumps({**info, 'running': owned(info), 'healthy': owned(info) and healthy(info['port'])}, ensure_ascii=False))
        else:
            info = start(args.port)
            if args.command == 'start':
                print(json.dumps(info, ensure_ascii=False))
            else:
                arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
                return subprocess.call(cli_args(info['port'], arguments))
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
