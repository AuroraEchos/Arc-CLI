# Arc CLI v0.1.0

Arc CLI 0.1.0 是当前 Runtime 基线的首个正式 GitHub Release。本次变更归入 `0.1.0` 的稳定与
修复范围。

## Highlights

- 建立 `Model proposes → Arc authorizes → Tool executes` 的 Runtime 授权链路；
- Tool subprocess 默认移除 Arc Provider credentials 与常见 ambient secrets；
- Provider 配置统一为 `ARC_MODEL`、`ARC_BASE_URL` 和 `ARC_API_KEY`；
- 引入 Arc Core、Developer Profile、User 与 Workspace 的指令信任边界；
- 支持 `ARC.md`，并保留 Developer Profile 对 `AGENTS.md` 的支持；
- 稳定 Runtime Event Protocol v1 及 Agent/Turn/Message/Tool 生命周期；
- 加固 Session reopen、branch、checkpoint、取消和 incomplete tool-call recovery；
- 改进终端 streaming、steering prompt、tool status 和常用 Markdown 渲染；
- 增加真实 PTY、Provider、Policy、Secret isolation 与 Session semantics 测试。

## Configuration change

旧的通用 Provider 环境变量不再读取：

```text
MODEL
BASE_URL
API_KEY
```

请改用：

```text
ARC_MODEL
ARC_BASE_URL
ARC_API_KEY
```

项目不会自动设置 `NO_PROXY`。如某个开发环境确实需要代理例外，应由该环境自行配置。

## Install

推荐使用 uv 安装 Release wheel：

```bash
uv tool install "https://github.com/AuroraEchos/Arc-CLI/releases/download/v0.1.0/arc_cli-0.1.0-py3-none-any.whl"
```

需要 Python 3.11 或更高版本，以及 Linux 或 macOS 环境。
