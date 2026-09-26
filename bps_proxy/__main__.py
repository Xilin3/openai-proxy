"""Run the local basispoints proxy."""

from __future__ import annotations

import argparse
import ipaddress
import logging
from pathlib import Path

from bps_proxy.server import serve
from bps_proxy.wire import CallMemory, DEFAULT_MODEL


def main() -> None:
    parser = argparse.ArgumentParser(
        description="把本机 ChatGPT 的 Responses 请求转到 Excel 插件后端，并用 run_officejs 转接工具。"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--state",
        type=Path,
        default=Path.home() / ".bps-proxy" / "calls.json",
        help="记住 run_officejs 调用，方便工具结果回放",
    )
    args = parser.parse_args()
    try:
        valid_host = args.host == 'localhost' or ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        valid_host = False
    if not valid_host:
        parser.error('--host must be a loopback address or localhost')
    if not 1 <= args.port <= 65535:
        parser.error('--port must be between 1 and 65535')
    address = f'[{args.host}]' if ':' in args.host else args.host
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(
        "\n".join(
            [
                f"代理已准备监听 http://{args.host}:{args.port}/v1",
                f"默认模型 {DEFAULT_MODEL}。effort 的 max 会映射成 xhigh。",
                "在 ~/.codex/config.toml 里加上：",
                "",
                'model_provider = "bps"',
                "",
                "[model_providers.bps]",
                'name = "Basispoints"',
                f'base_url = "http://{address}:{args.port}/v1"',
                'wire_api = "responses"',
                "",
            ]
        ),
        flush=True,
    )
    serve(args.host, args.port, CallMemory(args.state))


if __name__ == "__main__":
    main()
