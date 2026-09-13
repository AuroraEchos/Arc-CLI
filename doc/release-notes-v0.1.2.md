# Arc CLI v0.1.2

Arc CLI 0.1.2 增加 DeepSeek Chat Completions 思考控制和实时 token usage 展示，并改善交互终端的
输入层次与可读性。

## Highlights

- 新增 `--thinking enabled|disabled`，DeepSeek 默认显式关闭思考模式；
- 新增 `--reasoning-effort none|low|high|max`，并兼容 `minimal`、`medium`、`xhigh` 映射；
- 新增 DeepSeek `max_tokens` 支持，范围为 1 到 393216，`--max-output-tokens` 保留为兼容别名；
- 保留并回传 DeepSeek `reasoning_content`，支持思考模式下的连续工具调用；
- 完整处理 `content_filter`、`insufficient_system_resource` 和 `aborted` 停止原因；
- 每轮模型响应后在底部状态栏展示输入、输出和总 token，多轮任务同时展示累计 usage；
- 为用户输入区增加上下分隔线和垂直留白，降低输入、回复与工具摘要之间的视觉拥挤；
- 保持普通 OpenAI-compatible Provider 的兼容性，未启用 DeepSeek 参数时不发送 `thinking`；
- 移除与 Arc Runtime 无关的顶层 `detect_cycle.py` 干扰文件；
- 新增 Provider、Agent、Session、终端和真实 PTY 回归测试。

## Install

```bash
uv tool install "https://github.com/AuroraEchos/Arc-CLI/releases/download/v0.1.2/arc_cli-0.1.2-py3-none-any.whl"
```

从旧版本升级时使用：

```bash
uv tool install --force "https://github.com/AuroraEchos/Arc-CLI/releases/download/v0.1.2/arc_cli-0.1.2-py3-none-any.whl"
```

需要 Python 3.11 或更高版本，以及 Linux 或 macOS 环境。
