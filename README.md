<div align="center">

<pre>
&#x20;█████╗  ██████╗   ██████╗
██╔══██╗ ██╔══██╗ ██╔════╝
███████║ ██████╔╝ ██║    &#x20;
██╔══██║ ██╔══██╗ ██║    &#x20;
██║  ██║ ██║  ██║ ╚██████╗
╚═╝  ╚═╝ ╚═╝  ╚═╝  ╚═════╝
</pre>

<p><strong>Arc CLI</strong></p>

<p>A terminal-native agent runtime.</p>

<p><code>branchable sessions</code> · <code>explicit context projection</code> · <code>validated tool execution</code></p>

<p><em>History is not context. Side effects are not memories.</em></p>

</div>

---

Arc CLI 是一个终端原生 Agent Runtime，支持可分支会话、显式上下文投影与可校验工具执行。

Arc 是运行在 Arc CLI 中的编程 Agent。它持续将对话发送给模型，按顺序执行模型请求的工具，
将可验证的工具结果写回历史，并继续推理，直到任务完成、被取消或达到轮次上限。

项目强调清晰的运行时边界和可审计行为：模型协议、Agent 循环、工具系统、上下文投影、
会话持久化和终端界面彼此独立，便于阅读、测试与扩展。

## 核心能力

- **终端原生**：支持交互模式、单次执行、标准输入和 JSONL 事件输出。
- **可分支会话**：会话以追加式 JSONL 树保存，可切换历史节点并从任意节点继续。
- **显式上下文投影**：持久化历史与发送给模型的上下文分离，可过滤本地消息并手动压缩。
- **可校验工具执行**：工具参数使用 JSON Schema 校验，工具结果和错误都会进入消息历史。
- **流式运行时**：统一输出模型文本、工具进度、工具结果、错误和生命周期事件。
- **安静的过程可见性**：单行状态动画与工具摘要让过程可见，但不会用原始输出刷屏。
- **可靠取消**：取消 shell 工具时清理整个进程组，并为未完成的工具调用补充明确结果。

## 运行要求

- Python 3.11 或更高版本
- Linux 或 macOS
- Bash
- 支持流式 function calling 的 OpenAI Chat Completions 兼容服务

项目使用 Python 3.12 作为主要开发环境，并推荐使用
[uv](https://docs.astral.sh/uv/) 管理依赖。

## 安装与配置

```bash
git clone git@github.com:AuroraEchos/Arc-CLI.git
cd Arc-CLI
cp .env.example .env
uv sync --extra dev
```

在 `.env` 中配置模型服务：

```dotenv
BASE_URL=https://api.example.com/v1
MODEL=your-tool-capable-model-id
API_KEY=your-api-key
# NO_PROXY=api.example.com
```

`BASE_URL` 应是包含 `/v1` 的 API 根路径，而不是完整的 `/chat/completions` 地址。
配置优先级为命令行参数、工作目录中的 `.env`、已有进程环境变量；密钥仅从
`API_KEY` 读取。`.env` 已被 Git 忽略，不应提交。

如果不使用 uv，也可以安装为普通 Python 项目：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

## 快速开始

启动交互模式：

```bash
uv run arc
```

执行一次只读任务：

```bash
uv run arc --tools read -p "阅读 README.md 并解释项目架构"
```

允许 Arc 修改项目并执行命令：

```bash
uv run arc --tools read,write,edit,bash -p "为这个项目实现一个小功能"
```

其他常用方式：

```bash
# 输出 JSONL 事件
uv run arc --no-session --mode json "展示运行时事件"

# 从标准输入读取提示词
printf '解释 Arc 的工具循环' | uv run arc --no-session

# 恢复当前工作目录最近的会话
uv run arc --continue

# 使用指定会话文件
uv run arc --session .arc/my-session.jsonl

# 在另一个工作目录中运行
uv run arc --cwd /path/to/project --tools read
```

## 终端体验

Arc CLI 遵循一条 UI 原则：**让用户看见 Agent 的过程，但不要让过程打扰用户。**

交互模式以 `arc ▸` 作为输入提示。模型请求、工具执行和上下文压缩期间，底部状态栏会使用
Braille spinner 在同一行显示 `thinking`、`running bash` 或 `compacting`。状态变化不会新增日志行，
也不会覆盖正在输入的内容。

模型文本使用轻量的 `arc │` 标识。工具完成后只显示名称、目标、状态和少量摘要；完整结果保留在
运行时中，可通过 `/last-tool` 查看。这样可以同时保留可观察性和阅读节奏。

颜色只用于身份、状态和错误层级。非 TTY 输出不会产生 ANSI 颜色；设置 `NO_COLOR` 环境变量或
传入 `--no-color` 可以显式关闭颜色。单次执行只向标准输出写入模型正文，JSON 模式保持机器可读。

交互输入支持上下键历史搜索，并可通过 Tab 补全斜杠命令。持久化会话的输入历史保存在
`.arc/input-history` 中。

## 交互命令

| 命令 | 行为 |
| --- | --- |
| `/help` | 显示交互命令帮助 |
| `/abort` 或 Ctrl-C | 取消当前任务并清理正在运行的 shell 进程组 |
| `/steer 内容` | 在下一次模型请求前加入补充指令 |
| `/follow-up 内容` | 当前任务自然结束后处理后续问题 |
| `/status` | 查看模型、工作目录、会话、分支、工具和运行状态 |
| `/last-tool [N]` | 查看最近一次完整工具结果，或结果的前 N 行 |
| `/history` | 查看当前分支的分层消息摘要 |
| `/tree` | 查看 Git 风格会话树和当前节点 |
| `/branch ID` | 切换到指定节点，后续消息形成新分支 |
| `/new` | 从空上下文开始，旧历史仍保留 |
| `/compact [N]` | 总结较早历史，保留最近 N 个用户轮次 |
| `/session` | 查看当前会话文件路径 |
| `/tools` | 查看启用的工具 |
| `/model [NAME]` | 查看或在空闲时切换模型 |
| `/clear` | 清除交互终端屏幕 |
| `/quit` 或 Ctrl-D | 退出 Arc CLI |

运行中的普通文本等同于 steering，只在模型轮次边界生效，不会中断已经开始的工具。
follow-up 会在当前工具循环自然结束后进入上下文。取消或失败后，待处理队列会被清空。

## 命令行约定

默认启用 `read,write,edit,bash`。使用 `--tools read` 可以限制为只读工具，
使用 `--tools none` 可以禁用全部工具。工具按模型给出的顺序执行，使
`read → edit → bash` 的依赖和副作用顺序保持可观察。

默认最多执行 20 次模型调用，每次输出上限为 4096 tokens。可以通过
`--max-turns` 和 `--max-output-tokens` 调整。`--timeout` 控制模型网络超时；
`bash` 工具有独立的 timeout 参数，默认 120 秒，最大 600 秒。

退出码：

- `0`：任务正常完成。
- `1`：模型错误或达到轮次上限。
- `2`：配置或应用层错误。
- `130`：用户取消单次任务。

## 架构

| 模块 | 职责 |
| --- | --- |
| [`types.py`](src/arc_cli/types.py) | 消息、工具、事件、用量与 Provider 协议 |
| [`providers.py`](src/arc_cli/providers.py) | OpenAI 兼容 HTTP/SSE 协议适配与测试替身 |
| [`agent.py`](src/arc_cli/agent.py) | Arc 多轮工具循环、事件、取消和任务队列 |
| [`tools.py`](src/arc_cli/tools.py) | 工具注册、参数校验以及内置文件和 shell 工具 |
| [`hooks.py`](src/arc_cli/hooks.py) | 工具执行前拦截与执行后结果转换 |
| [`context.py`](src/arc_cli/context.py) | 历史到模型上下文的投影和摘要压缩 |
| [`session.py`](src/arc_cli/session.py) | JSONL 会话树、分支、恢复、检查点与文件锁 |
| [`runtime.py`](src/arc_cli/runtime.py) | Arc 与会话持久化的组合层 |
| [`terminal.py`](src/arc_cli/terminal.py) | 品牌界面、状态动画、工具摘要和历史展示 |
| [`cli.py`](src/arc_cli/cli.py) | 参数解析、配置加载和交互命令调度 |

核心依赖方向保持单向：终端层消费运行时事件，运行时组合 Arc 与会话存储，Arc 只依赖
Provider、工具和统一消息类型。Provider 不执行工具，会话存储也不参与模型决策。

## 会话与上下文

会话默认保存在工作目录的 `.arc/sessions/*.jsonl` 中；`--no-session` 使用纯内存历史。
每个会话节点都有 `id` 和 `parent`。分支操作只追加 cursor 记录，不删除旧节点，也不会回滚
已经发生的文件修改或命令副作用。

压缩操作只追加 checkpoint，原始历史仍保留在文件中。恢复时沿当前 parent 链重建消息。
如果上次运行停在工具调用中间，Arc 会补充“结果未知”的错误消息，绝不会自动重放工具。

会话文件可能记录提示词、模型回复、源码、工具参数和工具结果。新建文件权限为 `0600`，
并使用排他锁限制为单写入者。不要上传或提交包含敏感数据的会话文件。

Arc CLI 默认读取工作目录中的 `AGENTS.md`，文件上限为 32 KiB；使用 `--no-context`
可以禁用。`/compact` 会调用模型并可能产生费用。

## 安全边界

Arc CLI 是本机开发工具，不是安全沙箱，也不包含完整的权限审批系统。内置工具可解析绝对路径，
`bash` 继承当前进程的环境与用户权限。即使只启用 `read`，也不应将其视为目录隔离机制。

建议先使用 `--tools read`，只在可恢复的代码仓库中启用写入和 shell 工具。连接远端模型后，
提示词、项目指令和工具读取的内容可能成为模型请求的一部分。

`write` 使用同目录临时文件和原子替换，`edit` 默认要求唯一精确匹配。二者都不是备份或跨文件事务。
文件读写上限为 1 MiB，单次工具输出上限为 24,000 个字符。`bash` 会清理子进程组，
不适合启动长期后台服务。

## 开发

推荐阅读顺序：

1. [`types.py`](src/arc_cli/types.py) 与 [`agent.py`](src/arc_cli/agent.py)
2. [`tools.py`](src/arc_cli/tools.py) 与 [`providers.py`](src/arc_cli/providers.py)
3. [`context.py`](src/arc_cli/context.py) 与 [`session.py`](src/arc_cli/session.py)
4. [`runtime.py`](src/arc_cli/runtime.py)、[`terminal.py`](src/arc_cli/terminal.py) 与
   [`cli.py`](src/arc_cli/cli.py)

运行质量检查：

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

测试使用临时目录、脚本化 Provider 和本地 SSE 模拟服务，不读取真实 API Key，也不会访问外部模型服务。
端到端场景通过真实 PTY 验证启动界面、动态状态、工具成功与失败、历史、会话树和 Ctrl-C 取消。

## 协议支持

当前实现支持 OpenAI Chat Completions 兼容协议，包括流式文本、function calling、
`stream_options.include_usage` 和 `max_completion_tokens`。目前不支持 Responses、
Anthropic 原生协议、图像、音频、推理签名或 OAuth。不兼容字段应在 Provider 层适配。

## 来源与许可

Arc CLI 是独立实现，不是 Pi 官方项目，也不保证协议、会话文件或扩展接口兼容。
初始设计参考 Pi 提交 `6160683a4a8012f0d1cd30c145df18b4ca6f5176` 的
[agent-loop.ts](https://github.com/earendil-works/pi/blob/6160683a4a8012f0d1cd30c145df18b4ca6f5176/packages/agent/src/agent-loop.ts)。

项目采用 MIT 许可证，详见 [LICENSE](LICENSE)；上游声明见 [NOTICE](NOTICE)。
