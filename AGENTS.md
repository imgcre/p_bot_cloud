# AGENTS.md

## Python

本机有真正的 Python，但是 `python` 只是 Windows Apps 的占位程序。需要运行 Python 时使用 `uv`，例如 `uv run python ...`。

## 换行符与文件编辑

- 不要为了“整理格式”主动转换整文件换行符。本仓库可能存在 CRLF/LF 混用或 Git 索引与工作区换行表现不一致的文件。
- 小范围代码修改优先使用 `apply_patch`，并尽量只改目标行；不要用 PowerShell/Python 读完整文件再 `WriteAllText`、`Set-Content`、`Out-File` 重写整个文件，除非任务明确要求换行归一化。
- 编辑前后用 `git diff -- <file>` 检查 diff 是否只包含真实逻辑改动；如果看到整文件都被标记为变更，通常是换行符被批量改写了。
- 提交或结束前对已改文件运行 `git diff --check -- <file...>`。若出现大量无关 trailing whitespace 或整文件 diff，先恢复到 Git 索引/HEAD 的原始内容，再重新应用最小补丁。
- 如果确实需要修改换行策略，必须把它作为独立改动说明，不要和业务逻辑修复混在一起。
