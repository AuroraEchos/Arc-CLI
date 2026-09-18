# Arc CLI v0.1.3

Arc CLI 0.1.3 进一步简化交互终端 transcript，使用户输入、Agent 正文和工具执行结果保持清晰，
同时减少重复的视觉元素。

## Highlights

- 输入区只保留 `›` 和 `↳` 提示符，移除上下边框与额外垂直留白；
- Agent 正文不再重复添加 `arc │` 身份标识或续行 gutter，由终端自然处理自动换行；
- 任务正常完成后保留一个空行，以最小视觉成本分隔回答与下一次输入；
- 工具摘要根据真实终端宽度裁剪，窄窗口优先保留工具名、执行结果和耗时；
- 宽终端继续展示完整工具目标与结果摘要，不损失原有信息；
- 保持流式尾部预览、Markdown 渲染、steering、预输入保护和非 TTY 输出行为不变；
- 新增并更新终端单元测试与真实 PTY 回归场景。

## Install

```bash
uv tool install "https://github.com/AuroraEchos/Arc-CLI/releases/download/v0.1.3/arc_cli-0.1.3-py3-none-any.whl"
```

从旧版本升级时使用：

```bash
uv tool install --force "https://github.com/AuroraEchos/Arc-CLI/releases/download/v0.1.3/arc_cli-0.1.3-py3-none-any.whl"
```

需要 Python 3.11 或更高版本，以及 Linux 或 macOS 环境。
