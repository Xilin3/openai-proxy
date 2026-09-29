# CPA 原生 BPS Excel 插件

这是 `kokojacket/openai-proxy` 核心协议的 Go 原生移植版，CPA 直接加载动态库。
运行时不依赖 Python，不启动独立代理进程，也不新增监听端口。

## 文件

- Linux x86_64：`dist/linux-amd64/bps-excel.so`
- Windows x86_64：`dist/windows-amd64/bps-excel.dll`
- CPA 配置示例：`config.example.yaml`
- 专用凭据格式：`auth.example.json`
- 完整实现范围、差异与源码构建方法：`README.md`

当前适配 CPA v8 的 C ABI 1 / schema 6，以及宿主 HTTP 操作和流式回调。
旧版 CPA 不保证兼容，不能仅凭“支持插件”判断能否使用。
Linux 产物需要 glibc 2.34+，不能直接用于 Alpine/musl。

## 安装

1. 确认 CPA 版本支持上述接口，并支持 CGO 原生插件。
2. 将对应平台的动态库放入 CPA 配置的插件目录，保持文件名不变。
3. 将下面的配置合并到现有配置中，不要覆盖其他插件或认证设置：

```yaml
plugins:
  enabled: true
  dir: plugins
  configs:
    bps-excel:
      enabled: true
      priority: 10
      max_concurrent: 8
      max_sse_event_mib: 16
      timeout_seconds: 900
```

4. 在 CPA 的认证目录内创建独立凭据文件，例如 `bps-excel-account.json`：

```json
{
  "type": "bps-excel",
  "tokens": {
    "access_token": "替换成你自己的有效 access_token",
    "account_id": "替换成对应 account_id"
  }
}
```

凭据文件应仅允许 CPA 运行用户读取。不要把令牌发到聊天里或提交到 Git。
本插件不会读取或修改本机 Codex 登录文件，也不会接管原有 `codex` 凭据。

5. 重启 CPA，检查插件加载日志和经过认证的 `/v1/models`。
6. 使用 CPA 原有 API Key 请求 `/v1/responses`，选择如
   `gpt-6-astra-excel`、`gpt-5.6-sol-excel` 的模型名。

全部模型都加了 `-excel` 后缀，避免与原有 provider 重名。
`gpt-6-sol-excel`、`gpt-6-luna-excel`、`gpt-6-terra-excel`
分别映射到对应的 5.6 型号；`max` 推理档位映射到 `xhigh`。

## 已实现

- Responses 同步/流式调用、模型和推理档位映射。
- Function/custom 工具、namespace 和 Lite `additional_tools`。
- `run_officejs` 封装与还原、函数参数 JSON Schema 校验。
- 只有收到有效的 `response.completed` 才释放客户端工具调用。
- 图片格式/尺寸校验、内联图片被拒绝后的附件上传重试。
- 使用 CPA 的 HTTP 传输，保留其代理与取消机制。
- 并发、请求发起速率、总超时及 SSE 大小限制。
- HTTP 错误码保留；插件停用时取消请求并等待清理。

## 重要限制

这是首版原生实现，并非原 Python 项目的所有功能都已等价移植：

- 暂无自动刷新令牌和交互式登录。令牌过期返回 401，需要更新凭据。
- 每次请求需携带完整历史和工具目录；不支持 `previous_response_id`、
  `item_reference`，也不提供跨请求调用缓存。
- 未移植原项目的自动工具修复重试、宽松 JavaScript 解析和自动工具推断。
  未知或不合法调用会明确报错，不会默默执行或假装成功。
- 不支持内置搜索工具、远程图片 URL、精确 token 计数和独立 compact 端点。
- 达到并发上限直接返回 503，没有等待队列。
- 其他客户端协议的转换依赖 CPA 自身，插件只声明 Responses 输入/输出。

## 验证范围

已在本机完成 Windows/Linux 单元测试、竞态检测及原生 ABI 冒烟测试，
并运行 `go vet`。冒烟测试实际加载 `.dll` / `.so`，但使用的是模拟 CPA
宿主和虚拟凭据，没有发送真实上游请求。

尚未部署到你的服务器，也未验证真实账号的上游可用性或已安装 CPA 的
实际兼容性。服务上线前仍需在目标 CPA 上做一次真实请求验证。

本次构建和测试记录见 `VERIFICATION.md`。
