# Arc Tool 开发指南：以 `apply_patch` 为例

本文记录 Arc CLI 0.1.1 中 `apply_patch` Tool 从需求分析、接口设计、实现、注册、授权、测试到发布的
完整过程。目标不是只解释一个 Tool，而是提供一套可以复用于 `search_files`、`git_status`、Web 或
MCP Tool 的开发方法。

## 1. 先理解 Tool 在 Runtime 中的位置

Arc 的基本执行链是：

```text
User request
  → Model proposes ToolCall
  → ToolRegistry validates name and JSON Schema
  → Tool resolves effects from validated arguments
  → ExecutionPolicy authorizes effects
  → Hooks.before_tool
  → Tool implementation executes
  → Hooks.after_tool
  → ToolResult returns to the model and Session
```

因此 Tool 的职责不是决定自己是否有权限执行。Tool 只需要：

1. 声明稳定、清晰的输入契约；
2. 根据参数准确声明副作用；
3. 执行一个边界明确的动作；
4. 返回模型能够理解和继续使用的结果；
5. 失败时不隐瞒状态，也不破坏 ToolCall/ToolResult 配对。

## 2. 为什么选择 `apply_patch`

0.1.0 已有四个 Tool：

```text
read    读取 UTF-8 文件
write   创建或覆盖完整文件
edit    执行一次精确文本替换
bash    执行 shell 命令
```

这些工具已经能修改代码，但复杂改动需要多次 `edit`。如果第三次修改失败，前两次已经写盘，模型需要
自行判断哪些步骤成功。`apply_patch` 填补的是“一组关联文件修改”的能力：

- 一个 ToolCall 可以操作多个文件；
- 在任何写盘发生前先验证所有操作；
- 同一个文件可以包含多个顺序精确替换；
- 提交阶段失败时尽力回滚已经完成的文件操作；
- 删除文件时向 Runtime 明确声明 destructive effect。

## 3. 先写 Tool Contract

开始编码前，先回答以下问题：

| 问题 | `apply_patch` 的答案 |
| --- | --- |
| 用户目标是什么 | 一次安全地应用一组关联文本文件变更 |
| 输入是什么 | `operations` 数组 |
| 支持什么动作 | `add`、`update`、`delete` |
| 默认副作用是什么 | `write` |
| 哪些参数改变风险 | 包含 `delete` 时增加 `destructive` |
| 文件边界是什么 | UTF-8 文本，结果不超过 1 MiB |
| 如何处理歧义 | `old_text` 必须恰好匹配一次 |
| 如何处理部分失败 | 预检后提交，提交失败时逆序回滚 |
| 输出是什么 | 操作数量、动作、文件路径和 edit 数量 |

本版本使用结构化 JSON，而不是立即实现完整 unified diff parser：

```json
{
  "operations": [
    {
      "type": "add",
      "path": "src/new_module.py",
      "content": "..."
    },
    {
      "type": "update",
      "path": "src/existing.py",
      "edits": [
        {
          "old_text": "old implementation",
          "new_text": "new implementation"
        }
      ]
    },
    {
      "type": "delete",
      "path": "src/obsolete.py"
    }
  ]
}
```

结构化 operations 更适合当前基于 Chat Completions function calling 和 JSON Schema 的 ToolRegistry，
也避免在第一个版本中引入复杂而容易出现兼容差异的 diff parser。

## 4. 用 JSON Schema 拒绝不合法输入

Tool Schema 位于 `create_builtin_tools()`。三个 operation 使用 `oneOf` 区分，并为每种操作设置：

- 明确的 `required` 字段；
- `additionalProperties: false`；
- 非空路径；
- 非空 `old_text`；
- operations 与 edits 数量上限；
- 字符串长度的初步限制。

Schema 的作用是阻止类型错误和结构错误进入授权与执行阶段。例如：

```json
{"operations": []}
```

以及：

```json
{
  "operations": [
    {
      "type": "update",
      "path": "a.py",
      "edits": [{"old_text": "", "new_text": "x"}]
    }
  ]
}
```

都会在 ToolRegistry validation 阶段失败。不要因为有 JSON Schema 就省略执行期检查：UTF-8 字节大小、
文件是否存在、文本匹配次数和并发状态只能在执行期确认。

## 5. 分离 Preflight 与 Commit

实现分成两个主要阶段。

### 5.1 Preflight

`_prepare_patch()` 读取并验证全部 operations，但不写盘：

1. 相对于 ToolContext.cwd 解析路径；
2. 拒绝同一请求中的重复规范化路径；
3. add 要求目标不存在；
4. update/delete 要求目标是可读取的 UTF-8 普通文件；
5. update 的每个 `old_text` 在当前计算结果中必须恰好出现一次；
6. 所有替换按数组顺序应用到内存字符串；
7. 拒绝没有产生变化的 update；
8. 检查最终 UTF-8 内容不超过 1 MiB；
9. 保存原始内容和权限，为 rollback 做准备。

如果任何 operation 预检失败，函数不会进入 commit，因此磁盘状态完全不变。

### 5.2 Commit

`_commit_patch()` 在每个文件真正提交前再次检查 precondition：

- add 的目标仍然不存在；
- update/delete 的内容仍与 preflight 时一致。

这可以发现预检后到提交前发生的大多数外部修改。add/update 复用 Arc 已有的同目录临时文件加
`os.replace()` 原子写入；delete 使用 `unlink()`。

文件系统通常不提供跨多个文件的一次原子事务，所以这里不能承诺严格的 multi-file atomicity。Arc
提供的是：完整预检、单文件原子替换和提交失败后的 best-effort rollback。

## 6. Rollback 设计

每成功提交一个 operation，就把对应 `_PreparedPatch` 加入 committed 列表。如果后续操作失败：

1. 逆序遍历 committed；
2. add 通过删除新文件恢复；
3. update 通过写回原始内容恢复；
4. delete 通过重建原始内容并恢复权限恢复；
5. rollback 自身失败时，将原始错误和恢复失败路径一起返回。

逆序非常重要。例如先新增目录中的文件、再更新另一个文件时失败，恢复顺序应与提交顺序相反。

需要诚实说明：进程突然断电、文件系统故障或并发程序在 rollback 期间继续修改文件时，best-effort
rollback 仍可能无法完全恢复。真正的跨文件事务需要日志、临时 staging 和崩溃恢复协议，超出 0.1.1
的 Tool 边界。

## 7. 动态 Effect 解析

Tool 的风险不能只由名称决定。`apply_patch` 的 effect 根据 operations 动态计算：

```text
只有 add/update  → write
包含 delete      → write + destructive
```

`_apply_patch_effects()` 在 Schema 校验后、Tool 实现执行前运行。这样 restricted policy 可以阻止或请求
确认删除操作，而普通文件更新仍按 write 处理。

Effect annotation 只是风险声明，真正的授权仍由 ExecutionPolicy 完成。Tool 实现不能通过 prompt 或
返回文本给自己授权。

## 8. 注册 Tool 并暴露给 Profile

实现函数本身不会让模型看到 Tool，还需要完成两处连接：

1. 在 `create_builtin_tools()` 中注册名称、描述、Schema、执行函数、默认 effects 和 resolver；
2. 在 `DEVELOPER_PROFILE.tool_names` 中加入 `apply_patch`。

Tool 描述应说明用户结果和关键边界，不要只重复名称。本次描述为：

```text
Apply validated add, update, and delete operations across UTF-8 text files as one patch.
```

默认工具顺序为：

```text
read → write → edit → apply_patch → bash
```

保留 `edit` 很有价值：单个简单替换使用 edit 更小、更清晰；关联的多文件变更才使用 apply_patch。

## 9. 测试策略

新增 Tool 至少应覆盖四层测试。

### 9.1 Schema tests

- 缺少 required 字段；
- 空 operations；
- 空 old_text；
- 多余字段；
- 错误类型或超出数量限制。

### 9.2 Success tests

- 同一调用 add、update、delete 多个文件；
- update 包含多个 edits；
- 内容正确；
- 原文件权限保持；
- ToolResult 摘要正确。

### 9.3 Failure invariants

- add 目标已存在；
- update 匹配为零；
- update 匹配多次；
- 重复规范化路径；
- 二进制文件；
- 超过大小限制；
- preflight 失败时所有文件不变；
- commit 中途失败时已提交操作被恢复。

### 9.4 Authorization tests

- add/update 解析为 write；
- delete 解析为 write + destructive；
- restricted policy 对 delete 返回 confirmation_required；
- 未授权时 Tool 实现不执行。

## 10. 文档、版本和发布

新增用户可见 Tool 后同步更新：

- `README.md` 的能力、默认工具和命令示例；
- `doc/architecture-design.md` 的 Profile/Tool 说明；
- 新版本 release notes；
- `src/arc_cli/__init__.py` 与 `pyproject.toml` 版本；
- `uv.lock` 中的项目版本；
- 依赖版本号的 CLI 和终端测试。

发布前执行：

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
uv lock --check
uv build
```

确认 wheel 中的版本与依赖后，再提交、创建 `v0.1.1` annotated tag、推送并创建 GitHub Release。

## 11. 开发下一个 Tool 的模板

开发任何新 Tool 前，先填写以下清单：

```text
Tool name:
User outcome:
When the model should use it:
Required inputs:
Optional inputs and defaults:
Output contract:
Static effects:
Argument-dependent effects:
Size/time/output limits:
Cancellation behavior:
Idempotency behavior:
Partial-failure behavior:
Sensitive data boundary:
Schema tests:
Success tests:
Failure-invariant tests:
Authorization tests:
Documentation updates:
```

然后按固定顺序开发：

```text
Contract
  → Schema
  → Pure/preflight logic
  → Side-effect commit
  → Error and rollback semantics
  → Effect resolver
  → Registry
  → Profile exposure
  → Tests
  → Docs
  → Full verification
  → Release
```

## 12. 常见错误

### 把所有功能都塞进 Bash

Bash 很灵活，但模型难以准确声明隐藏在命令字符串中的副作用。高频、结构稳定的动作应成为独立 Tool。

### 只测试成功路径

Agent Runtime 最重要的是失败后的事实状态。测试应优先证明“失败时没有执行”或“已执行部分得到明确
恢复和记录”。

### 在 Tool 内部决定授权

Tool 可以解析 effects，但不能批准自己。授权必须在实现执行前由 Runtime Policy 完成。

### Schema 与执行逻辑不一致

Schema 是模型看到的公开契约。修改实现时必须同步检查 Schema、description、tests 和文档。

### 声称跨文件绝对原子

单文件可以通过 `os.replace()` 原子替换；多个文件没有通用的单次原子提交。应准确使用“预检 + 单文件
原子写入 + best-effort rollback”描述实际保证。

## 13. 本次涉及的代码位置

- `src/arc_cli/tools.py`：实现、Schema、effect resolver 和注册；
- `src/arc_cli/profiles.py`：Developer Profile 默认启用；
- `tests/test_tools.py`：Schema、成功、失败、rollback 和 authorization 测试；
- `README.md`：用户可见用法；
- `doc/architecture-design.md`：架构边界；
- `doc/release-notes-v0.1.1.md`：发布说明。

这套流程的核心原则是：先定义可验证的行为和失败不变量，再写产生副作用的代码。Tool 的数量不是
Agent 能力的关键，稳定契约、准确授权和可恢复失败才是。
