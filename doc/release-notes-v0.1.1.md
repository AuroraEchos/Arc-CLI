# Arc CLI v0.1.1

Arc CLI 0.1.1 增加结构化文件补丁能力，并将本轮配置与本地状态边界改进作为新的补丁版本发布。

## Highlights

- 新增 `apply_patch` Tool，支持在一次调用中 add、update 和 delete 多个 UTF-8 文本文件；
- 所有 patch operations 在写盘前统一完成 Schema、路径、文件状态、大小和文本匹配预检；
- update 支持同一文件中的多个顺序精确替换，缺失或歧义匹配会使整个 patch 在写盘前失败；
- 提交阶段失败时逆序恢复已经应用的变更，并明确报告无法完成的 rollback；
- delete 同时声明 `write` 与 `destructive` effects，保留 Runtime policy 授权边界；
- Provider 配置不再读取项目 `.env`，`ARC_API_KEY` 仅来自 Arc 进程继承的环境；
- 默认 Session 与输入历史迁移到 `$XDG_STATE_HOME/arc/`，按工作目录哈希隔离；
- System Prompt 在每次模型请求前重新渲染，使当前系统时间保持准确。

## Install

```bash
uv tool install "https://github.com/AuroraEchos/Arc-CLI/releases/download/v0.1.1/arc_cli-0.1.1-py3-none-any.whl"
```

从旧版本升级时使用：

```bash
uv tool install --force "https://github.com/AuroraEchos/Arc-CLI/releases/download/v0.1.1/arc_cli-0.1.1-py3-none-any.whl"
```

需要 Python 3.11 或更高版本，以及 Linux 或 macOS 环境。
