# bps-proxy

把本机 Codex 等客户端的 Responses 请求转到 Excel 插件后端。客户端自己的工具会经 `run_officejs` 转接，仍由客户端执行。

社区交流：[LINUX DO](https://linux.do/)。

这是独立的社区项目，与 OpenAI 没有隶属或背书关系。上游接口及账号可用性可能变化。

默认监听本机 `127.0.0.1`，也支持 `localhost` 和 IPv6 回环地址 `::1`；不允许绑定 `0.0.0.0` 等对外地址，也不接受浏览器网页直接请求。代理使用你的登录态访问上游，请只在可信的个人电脑上运行。

## 需要

- Python 3.9 或更高，没有第三方依赖
- 本机 Codex 已经登录。默认读取 `~/.codex/auth.json`；设置了 `CODEX_HOME` 时读取该目录下的 `auth.json`

## 安装

```bash
git clone https://github.com/kokojacket/openai-proxy.git
cd openai-proxy
```

可以直接从源码运行，也可以在 Python 虚拟环境中安装命令行入口：

```bash
python3 -m pip install .
bps-proxy --help
```

Windows 上使用 `python` 代替 `python3`。

## 启动

macOS / Linux：

```bash
./start.sh
```

Windows：

```bat
start.bat
```

也可以：

```bash
python3 -m bps_proxy
```

默认地址是 `http://127.0.0.1:8787/v1`。换端口：`python3 -m bps_proxy --port 8788`。

确认服务已启动：

```bash
curl http://127.0.0.1:8787/health
```

健康接口仅检查本机代理是否可访问，不检查登录态或上游可用性。日志输出到终端。端口被占用时，停止占用它的旧进程或换一个端口。

## 接到 Codex

把 `codex-config.toml` 里的两段贴进你的 `~/.codex/config.toml`。`model_provider` 放在文件顶层，不要放进别的 `[table]` 里面。

```toml
model_provider = "bps"

[model_providers.bps]
name = "Basispoints"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
```

改完重新开一次对话。代理没启动时，Codex 会连不上。

要回到官方通道，删掉 `model_provider = "bps"` 和 `[model_providers.bps]`。

## 并行验证新版本

macOS / Linux 上，可以在独立工作副本中启动候选服务：

```bash
python3 tools/candidate.py start
python3 tools/candidate.py status
python3 tools/candidate.py run
python3 tools/candidate.py stop
```

默认候选端口为 `18787`，缓存与日志位于 `~/.bps-proxy/candidates/<副本标识>/`。`run` 通过 Codex 的 `-c` 参数指定本次连接地址，不写入共享的 `~/.codex/config.toml`。ChatGPT 桌面端与独立 CLI 的现有连接配置不会因此改变。

若端口已被占用，使用 `python3 tools/candidate.py --port 18788 start` 和相同端口的 `run`。工具不会停止占用端口的其他服务，也不允许用候选模式绑定 `8787`。`stop` 只停止本副本启动的候选进程。

验证图片可以使用：

```bash
python3 tools/candidate.py run -- exec --image /path/to/image.png '描述这张图片'
```

完成验证后再安排桌面端切换；已有对话依赖原代理时，保持原进程和共享配置。候选缓存独立，切换后应开启新对话，不应假定能接续原代理缓存中的工具调用。

## 图片

支持用户消息中的图片，以及工具结果数组中的截图。PNG、JPEG、GIF、WebP 会检查编码、格式头部、尺寸及大小。单张最多 20 MiB，图片解码后合计最多 32 MiB；整个 JSON 请求仍受 32 MiB 限制，因此 base64 的编码开销也会计入请求体。

内联图片被上游拒绝时，会尝试上传到 BPS 附件接口并使用返回的附件编号。同账号的重复图片复用缓存，附件失效时重新上传；不需要自行搭建公网图片服务。图片会发送至 OpenAI 的 BPS 服务，代理不能控制该服务的数据保留期限。

图片格式错误、上传失败或附件仍被拒绝时会返回错误，不会省略图片后继续回答。工具结果中的相邻文字和图片数组会一并保留。

## 模型和档位

代理的模型目录列出：`gpt-6-astra`、`gpt-5.6-sol`、`gpt-5.6-luna`、`gpt-5.6-terra`。实际可用性取决于上游和账号权限。

请求 `gpt-6-luna` 时，代理固定转发到 `gpt-5.6-luna`。这条兼容映射适用于所有同名请求，包括后台标题生成；实际执行的是 `gpt-5.6-luna`。日志会分别记录请求型号和实际型号，其他型号不会因收到 403 而自动切换。

`low`、`medium`、`high`、`xhigh`、`ultra` 原样送出。这个后端没有 `max`，选这一档时代理会改成 `xhigh`。

## 请求与工具

- 提供 `POST /v1/responses`、`GET /v1/models` 和 `GET /health`，同时兼容省略 `/v1` 的响应与模型路径。
- `input` 使用字符串或消息数组；`stream` 使用布尔值。支持流式和非流式响应，也会保留上游的 `incomplete`、`failed` 状态。
- 支持普通函数工具及 `custom` 文本工具。参数类型、必填项、枚举和嵌套约束会转成自然语言工具目录，不向上游发送客户端 `tools` 或 `tool_choice` 字段。工具结果保留文本和图片内容；`tool_choice: "none"` 会禁用客户端工具，未声明的工具不会被自动启用。格式有歧义的可执行脚本会要求重新生成，不猜测补写引号。
- `tool_choice: "required"` 或指定工具时，必须返回有效的客户端调用；没有匹配工具返回 `400`，漏调用经一次纠正仍未恢复则返回 `response.failed`。上游的拒绝回答、`failed` 和 `incomplete` 状态保持原样。已有会话省略 `tools` 或用空数组续接时沿用原目录；明确禁用工具请用 `tool_choice: "none"`。
- 每次请求需携带完整对话历史。不支持 `previous_response_id`、`conversation` 和后台请求；这类请求会返回 `400`。
- 请求体上限为 32 MiB，需要 `Content-Length`，不接受分块上传；读取客户端请求体超时为 30 秒。这是 Responses API 的兼容子集，并非所有参数都已实现。

## 登录与缓存

登录态无效或过期时返回 `401`；请重新登录本机 Codex。代理不会自动刷新登录令牌。

新调用按账号和会话隔离。工具回放缓存默认位于 `~/.bps-proxy/calls.json`，可以通过 `--state /path/to/calls.json` 更换位置。缓存包含完整的原始调用（包括 `id`、`summary` 和 `references`）与轮次计数，请不要上传或分享；在 macOS / Linux 上，新写入的缓存仅允许当前用户读写。调用与轮次计数各最多保留 512 条，写盘失败时会记录警告并继续使用内存中的记录。缓存丢失或被淘汰后，重建的调用无法保证与原始调用完全一致。

同一轮工具续接保持 `turn_id`，内部重试会递增 `agent_iteration`；客户端重复提交同一阶段保持计数，收到下一阶段的工具结果后继续递增。计数会随缓存保存，避免内部重试后收到客户端工具结果时回退。旧格式缓存可读取，但无作用域的旧条目不会自动用于新账号或新会话。

普通文本增量转发。可执行工具调用只在上游正式完成响应后交付；断流、事件损坏或连续工具转接失败会保留失败状态，不合成成功响应。网络超时返回 `504`，其他连接错误返回 `502`；如果流式响应已经开始，则通过 SSE `response.failed` 事件报告。单条上游事件最多 4 MiB，空闲连接超时为 5 分钟，单次上游流最长为 15 分钟。

Excel 行为指令由后端注入，代理无法将其从模型实际收到的提示中删除。返回给客户端的 `instructions` 字段会还原为客户端原文，但这不代表后端提示词已被清除；该转接方式无法保证与官方 Responses API 完全一致。

## 开发验证

```bash
python3 -m unittest discover -s tests -v
```

测试使用临时文件、回环 HTTP 服务和模拟上游，不需要真实账号或外部请求。

## 实现参考

图片附件上传与重传流程参考 Kaixxrua/excel-codex-bridge（核对版本 `66c41df`）。请求限额、图片隔离与终态校验参考 ranxi2001/sub2api 的 BPS 通道（核对版本 `d215edd`）。本项目保留标准库实现，没有引入对方的服务端框架或公网图片中转。

## 贡献

欢迎通过 GitHub Issues 报告问题，或提交 Pull Request。问题报告请附上 Python 版本、操作系统、复现步骤和已脱敏的错误信息。请勿上传登录令牌、`auth.json`、对话内容或工具调用缓存。

## 许可证

本项目采用 [MIT License](LICENSE)。
