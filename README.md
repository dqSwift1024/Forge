# Forge

一个在命令行里完成编程任务的工具。配好 API key 后，用一句话描述任务，它会读文件、改代码、跑命令，直到做完。

## 运行

在项目根目录：

```bash
export PYTHONPATH=.
export FORGE_API_KEY=你的key
export FORGE_BASE_URL=https://api.openai.com
python -m forge run --provider openai --model gpt-4.1 --permission bypass -p "你的任务"
```

Anthropic：

```bash
export FORGE_API_KEY=你的key
export FORGE_BASE_URL=https://api.anthropic.com
python -m forge run --provider anthropic --model claude-sonnet-4-5 --permission bypass -p "你的任务"
```

不配 key 时可以用假模型。`--script` 按写好的步骤执行，`--decider` 根据工具结果自己决定下一步。

```bash
python -m forge run --provider fake --script turns.json --permission bypass -p "任务"
python -m forge undo --session-id <id>
```

## 配置优先级

环境变量 > 项目 `.forge/config.json` > `$FORGE_HOME/config.json`（默认 `~/.forge/config.json`）。

`FORGE_API_KEY`、`FORGE_BASE_URL`、`FORGE_MODEL`、`FORGE_PROVIDER`、`FORGE_MAX_TOOL_OUTPUT`、`FORGE_MAX_CONTEXT_CHARS`。

## 权限

- 不传 `--permission`：没有终端时拒绝 shell，并把拒绝结果交回模型。
- `plan`：不改源码，只写 `.forge/artifacts/`。
- `accept-edits`：可以改文件。shell 需要 `--allow-bash <前缀>`。
- `bypass`：允许普通命令。`sudo` 和包含 `rm -rf /` 的命令仍然拒绝。

项目根目录的 `AGENTS.md` 会进入每一轮请求。

## 测试

27 条验收测试是冻结的，实现时不要改它们。

```bash
python -m pytest tests/acceptance -q
```

只使用 Python 标准库。测试需要 pytest。
