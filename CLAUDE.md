# agent_memory_engine — Claude 工作规范

## Memory Engine MCP 使用规范

本项目自身就是一个 memory engine，开发时必须使用其 MCP 工具，确保知识积累形成闭环。

### 编码工作开始前（必须）

调用 `mcp__memory-engine__retrieve_agent_context`，传入当前任务描述、branch 和 head commit：

```
task: <当前任务的简要描述>
current_branch: <当前分支>
head_commit: <当前 commit SHA>
```

目的：获取相关历史记忆和知识 chunk，避免重复踩坑。

### 编码过程中（按需）

- **`mcp__memory-engine__inspect_knowledge`** — 需要查看特定源文件片段时调用，比直接 Read 更快定位已索引内容。
- **`mcp__memory-engine__inspect_memory`** — 当 retrieve 返回的某条 memory 摘要不够详细时，展开查看完整内容。
- **`mcp__memory-engine__refresh_project_knowledge`** — 新增或大幅修改文件后调用，保持索引同步。
- **`mcp__memory-engine__memory_status`** — 怀疑引擎状态异常时检查。

### 完成经过验证的工作后（必须）

调用 `mcp__memory-engine__reflect_and_write`，条件：
- 测试通过（`verification_status: tests_passed`）
- 构建成功（`build_success`）
- 人工确认（`manual_check`）

**不要在以下情况调用**：任务失败、已回滚、仅是探索性对话、微小改动。

```
task: <完成的任务描述>
outcome: <实际发生了什么>
verification_status: tests_passed | build_success | manual_check
current_branch: <分支>
head_commit: <commit SHA>
changed_files: [<修改的文件列表>]
```

### 工具加载提醒

memory engine MCP 工具默认为 deferred 状态，使用前需先通过 ToolSearch 加载：

```
ToolSearch: select:mcp__memory-engine__retrieve_agent_context,mcp__memory-engine__reflect_and_write,...
```
