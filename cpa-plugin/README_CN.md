# CPA 原生 BPS Excel 插件

这是 `kokojacket/openai-proxy` 核心协议的 Go 原生移植版，CPA 直接加载动态库。
运行时不依赖 Python，不启动独立代理进程，也不新增监听端口。

**v0.2.0：复用 CPA 已有 Codex 凭据，保留原模型名，启用后全局接管模型推理请求。**

## 文件

- Linux x86_64：`dist/linux-amd64/bps-excel.so`
- Windows x86_64：`dist/windows-amd64/bps-excel.dll`
- CPA 配置示例：`config.example.yaml`
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
      priority: 10000
      max_concurrent: 8
      max_sse_event_mib: 16
      timeout_seconds: 900
```

4. 在 CPA 中保持原有 **Codex OAuth 账号**可用即可，不需要再导入一份凭据、
   不要修改原文件的 `type: codex`。插件通过 CPA 的凭据接口读取文件，不写入
   或复制它们，也不保存 refresh token。普通 API Key 不是可复用的登录凭据。

5. 重启 CPA，检查插件加载日志和经过认证的 `/v1/models`。
6. 使用 CPA 原有 API Key 请求 `/v1/responses`，选择如
   `gpt-6-astra`、`gpt-5.6-sol` 的原始模型名。

不添加 `-excel` 后缀，返回给客户端的响应模型名也保持原样。原项目的上游
映射仍然保留：`gpt-6-sol`、`gpt-6-luna`、`gpt-6-terra` 分别发给对应的
5.6 型号；`max` 推理档位映射到 `xhigh`。

## 全局接管行为

插件成功加载并启用后，所有进入 CPA 模型路由的推理请求都交给本插件。
未知模型、缺少可用凭据、Excel 后端失败都会报错，**不会自动退回 Codex
原生或其他 provider**。没有协议转换器的请求也会被拦截拒绝。
这不会把健康检查、管理界面、登录或令牌刷新请求转到 Excel。

本插件应是最高优先级的模型路由插件；禁用其他竞争的全局路由插件。
CPA Home 模式不支持该直接插件路由，需使用普通本地 CPA 模式。
插件被禁用、加载失败或被 CPA 熔断时，不再控制宿主路由；应检查加载日志。

临时恢复 CPA 原有通道：禁用 `plugins.configs.bps-excel.enabled` 并重新加载
配置或重启 CPA，不需要改动账号和客户端模型名。

## 凭据和刷新

插件每次请求都会读取 CPA 当前的 Codex 凭据，先检查运行时状态，再读取文件：

- 跳过禁用、不可用、冷却中、非 Codex、仅内存账号以及无效/过期令牌。
- 优先选择高优先级账号，同优先级轮换；不跨请求缓存令牌。
- 保留 CPA 内置 Codex 登录和后台刷新流程；刷新写回后，下次请求直接使用新令牌。
- 不主动触发 OAuth 刷新，也不保证“收到 401 后立即刷新重试”。CPA 原有刷新
  失败时，仍需在 CPA 中重新登录。
- 这复用了凭据及其原有生命周期，**不是 CPA 原生调度器**；不继承全部会话粘性、
  模型级冷却、按账号用量统计及上游错误后的自动换号重试。
- 当前宿主接口只暴露文件凭据内容。无落盘文件的 Home/内存凭据不可用。
- 出站使用 CPA 全局代理；存在独立 `proxy_url` 的账号会被跳过，避免静默绕过
  账号指定的网络策略。

## 已实现

- Responses 同步/流式调用、模型和推理档位映射。
- 全局推理路由、原始模型名、CPA 现有 Codex OAuth 凭据复用。
- Function/custom 工具、namespace 和 Lite `additional_tools`。
- `run_officejs` 封装与还原、函数参数 JSON Schema 校验。
- 只有收到有效的 `response.completed` 才释放客户端工具调用。
- 图片格式/尺寸校验、内联图片被拒绝后的附件上传重试。
- 使用 CPA 的 HTTP 传输，保留其代理与取消机制。
- 并发、请求发起速率、总超时及 SSE 大小限制。
- HTTP 错误码保留；插件停用时取消请求并等待清理。

## 重要限制

这是首版原生实现，并非原 Python 项目的所有功能都已等价移植：

- 插件本身不接管登录/刷新，使用 CPA 原有功能；具体边界见上面的凭据说明。
- 每次请求需携带完整历史和工具目录；不支持 `previous_response_id`、
  `item_reference`，也不提供跨请求调用缓存。
- 未移植原项目的自动工具修复重试、宽松 JavaScript 解析和自动工具推断。
  未知或不合法调用会明确报错，不会默默执行或假装成功。
- 不支持内置搜索工具、远程图片 URL、精确 token 计数和独立 compact 端点。
- 达到并发上限直接返回 503，没有等待队列。
- 其他客户端协议的转换依赖 CPA 自身，插件声明 Responses/Codex Responses 输入输出。

## 验证范围

已完成 27 项 Windows/Linux Go 测试、竞态检测、原生 ABI 冒烟测试及 `go vet`。
另外使用实际 CPA 源码构建的 Linux 插件宿主，验证了加载注册、四种协议路由、
原有凭据读取、刷新执行器保留和回退拦截。所有测试只使用虚拟凭据，
没有发送真实上游请求。

尚未部署到你的服务器，也未验证真实账号的上游可用性或已安装 CPA 的
实际兼容性。服务上线前仍需在目标 CPA 上做一次真实请求验证。

本次构建和测试记录见 `VERIFICATION.md`。

## 从 v0.1.0 升级

替换动态库，采用上述配置并重启 CPA。客户端删去模型名中的 `-excel`。
旧的 `type: bps-excel` 专用凭据不再使用；原来的 `type: codex` 文件保持不变。
v0.2.0 会全局接管推理，不再只是新增独立模型通道。
