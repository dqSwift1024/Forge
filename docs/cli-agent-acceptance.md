# forge：CLI 编程代理的任务计划与冻结测试

本文件是计划。实现代码还没写。验收认两个冻结文件，编码时都不许改：

| 文件 | 条数 | SHA-256 |
|---|---|---|
| `tests/acceptance/test_forge_cli.py` | 20 | `ef8b967875595ad3141b4cb2b2c8e8081702b9840a81b3e252b1028cca32d4d3` |
| `tests/acceptance/test_forge_cli_autonomy.py` | 7 | `f4dcf96a4f088f126bd686ed11a2dc56c306dc42f64262af150815e6b4e4189a` |

完成的定义是：两个指纹都不变，并且 `python -m pytest tests/acceptance -q` 全部通过。只绿前 20 条不算完成。

## 资料

依据是 Claude Code 官方文档，不是源码（CLI 本体不公开实现）：

- [Overview](https://code.claude.com/docs/en/overview)：终端里读仓库、改文件、跑命令；`ANTHROPIC_API_KEY` 可跳过登录；`claude -p` 做非交互。
- [How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works)：代理环是 gather context → take action → verify。模型只推理，harness 提供工具和上下文。工具分文件、搜索、执行、网页、代码智能。权限模式含 plan / accept edits。改文件前做 checkpoint。`CLAUDE.md` 或 `AGENTS.md` 每次会话注入。上下文变满会压缩，单次工具输出过大要截断。
- [Headless / Agent SDK](https://code.claude.com/docs/en/headless)：同一套环可以不进人机交互，用一次提示跑完。

Cursor 和 Antigravity 没有把可照搬的内部模块公开。落到这个 CLI 里的是它们的技术职责，不是它们的界面：

| 来源 | 技术职责 | forge 里的对应 |
|---|---|---|
| Claude Code | 代理环、工具、权限、检查点、项目指令、非交互 | `run` 循环、六个工具、三种权限、`undo`、`AGENTS.md`、`-p` |
| Cursor | 检索重排、补丁对齐 | `grep` / `glob` 现查，不做向量库；`edit_file` 只替换唯一匹配 |
| Antigravity | 任务先计划、产物可复查 | `plan` 模式只写 `.forge/artifacts/`；每次运行留下 diff 和 summary |

不做：Tab 补全、编辑器、向量索引、浏览器点击、云端虚拟机、多代理调度界面。那些会把工具做成 IDE。验收信号用命令退出码和 stdout。

## 产品

名字：`forge`。运行：`python -m forge`。实现只允许 Python 标准库。测试只允许 pytest。配一把 API key 就能走真实模型；测试本身不访问外网。

```
python -m forge run \
  --provider fake|openai|anthropic \
  --script turns.json \
  --permission bypass|accept-edits|plan \
  --allow-bash PREFIX \
  --session-id ID \
  --trace requests.jsonl \
  --max-turns N \
  --model NAME \
  -p "任务"
python -m forge undo --session-id ID
```

配置优先级：进程环境变量 > 当前目录 `.forge/config.json` > `$FORGE_HOME/config.json`（未设置 `FORGE_HOME` 时是 `~/.forge/config.json`）。字段：`provider`、`model`、`api_key`、`base_url`。环境变量：`FORGE_PROVIDER`、`FORGE_MODEL`、`FORGE_API_KEY`、`FORGE_BASE_URL`、`FORGE_MAX_TOOL_OUTPUT`。

模型：

- `openai`：`POST {base}/v1/chat/completions`，`Authorization: Bearer <key>`，不使用 stream。
- `anthropic`：`POST {base}/v1/messages`，`x-api-key`，带 `anthropic-version`。
- `fake`：按 `--script` 的 `turns` 逐条执行，不联网。`type=tool` 执行工具，`type=message` 把 `text` 写到 stdout 并退出 0。

工具名固定：`read_file`、`edit_file`、`write_file`、`grep`、`glob`、`bash`。

权限：

- `plan`：拒绝修改源码和 bash，工具结果含 `permission denied`。允许写入 `.forge/artifacts/`。
- `accept-edits`：允许改文件。bash 默认拒绝，除非命令以 `--allow-bash` 的前缀开头。
- `bypass`：允许普通命令。命令字符串含 `rm -rf /`，或第一个词是 `sudo`，仍然拒绝。

退出码：`0` 模型正常说完；`1` 超过 `--max-turns` 或其他运行失败；`3` 网络模型缺少 API key（stderr 含 `api key`）。

会话和产物：

- `.forge/sessions/<id>.jsonl` 里每个工具一行，含 `tool` 和布尔 `ok`。
- `.forge/artifacts/<id>/diff.patch` 与 `summary.txt`。`summary.txt` 去掉首尾空白后等于模型最后一句。
- `--trace` 每一行是一次发给模型的请求，含 `model`。工具的 stdout 必须出现在后续请求里。
- 工作区根的 `AGENTS.md` 进入第一次请求。
- `undo --session-id` 把该次改过的文件恢复到编辑前。

## 阶段

0. 冻结工具环测试，再冻结自主完成测试。本阶段到此为止，不写 `forge` 包。
1. 配置解析与退出码 3。
2. OpenAI / Anthropic 适配。一次读文件不够，要能连续六轮工具调用。
3. 六个工具、输出截断、权限和拒绝名单。不传 `--permission` 时按 `ask`：没有终端就拒绝该工具并把 `permission denied` 交回模型，进程不挂起。
4. 唯一匹配补丁。匹配失败只返回错误，循环继续。编辑前检查点，`undo`。
5. 代理环。`--script` 是假模型。`--decider` 才是「一句任务自己做完」：每轮把目前的消息从 stdin 交给决策程序，执行它返回的一个动作。bash 结果里带 stdout 和 `exit_code=N`。
6. 上下文：超过 `FORGE_MAX_CONTEXT_CHARS` 时压缩旧工具输出为 `[compacted]`。`AGENTS.md` 仍在每一次请求里，包括压缩后的最后一轮。最近一次工具输出保留。
7. 会话可以 `--resume`。同一 session 的下一次 `-p` 看得到上一轮的用户话和模型答复。不存在的 session 退出码 1。
8. 产物：diff、summary，以及 stdout 上的最终答复。summary 就是最终答复。
9. 跑全部 27 条。任一指纹变了就停止，先恢复测试文件。

## 测试用例

| # | 测试 | 通过标准 |
|---|---|---|
| 1 | `test_missing_api_key_exits_3` | openai 且没有 key，退出码 3，stderr 含 `api key` |
| 2 | `test_config_precedence_env_over_project_over_home` | 模型名依次取 home、项目、`FORGE_MODEL` |
| 3 | `test_openai_auth_model_and_tool_schema` | Bearer、模型名、非 stream、六个工具名都在请求里 |
| 4 | `test_anthropic_auth_header` | `x-api-key`、`anthropic-version`、`/v1/messages` |
| 5 | `test_openai_tool_roundtrip_reads_file` | 第二次请求带上磁盘上的 `hello-from-disk` |
| 6 | `test_anthropic_tool_roundtrip_reads_file` | 同上，走 Anthropic tool_use |
| 7 | `test_fake_edit_replaces_single_match` | `return a - b` 变成 `return a + b`，其余字节不变 |
| 8 | `test_fake_edit_rejects_ambiguous_match` | 两处匹配时文件不变，trace 含 `ambiguous` |
| 9 | `test_fake_write_grep_glob` | 新建文件；grep 回传含 `alpha = ` 的行；glob 回传 `keep.py` |
| 10 | `test_bash_stdout_feeds_back` | 命令打印 `2*6173`，trace 里出现结果 `12346` |
| 11 | `test_permission_plan_does_not_mutate_source` | 源码不变；计划文件写成功；trace 含 `permission denied` |
| 12 | `test_accept_edits_blocks_bash_unless_allowed` | 默许改文件但 bash 不执行；`--allow-bash python` 后 stdout 进入 trace |
| 13 | `test_bypass_still_blocks_denylist` | `rm -rf /` 子串和 `sudo` 都不执行，不产生副作用文件 |
| 14 | `test_checkpoint_undo` | 改完是 `v = 2`，undo 后回到 `v = 1` |
| 15 | `test_agents_md_in_first_request` | 第一次请求含 `RULE_ALWAYS_ADD_HEADER` |
| 16 | `test_tool_output_truncated` | `FORGE_MAX_TOOL_OUTPUT=32` 时 trace 含 `[truncated]`，且没有 200 个 Z |
| 17 | `test_max_turns_stops` | 只执行前两处编辑，退出码 1，最终句子不出现 |
| 18 | `test_verify_loop_reruns_check` | trace 里先有 `CHECK_FAIL` 再有 `CHECK_OK`，结束后检查脚本退出码 0 |
| 19 | `test_artifacts_diff_and_summary` | `diff.patch` 含新行；`summary.txt` 等于 `summary-line` |
| 20 | `test_session_jsonl_records_tools` | 会话里有 `read_file` 和 `edit_file`，且 `ok` 是布尔 |

## 补上的完成能力

前 20 条只证明脚本里的动作会被执行。下面 7 条补「CLI 里输入一段话就把任务做完」还缺的部分。

| # | 测试 | 补上的差距 | 通过标准 |
|---|---|---|---|
| 21 | `test_autonomous_prompt_fixes_failing_check` | 不给步骤脚本。决策程序只能看已经发生的工具结果 | `add` 被改对；trace 里先有 `CHECK_FAIL` 和 `exit_code=1`，后有 `CHECK_OK` 和 `exit_code=0`；stdout 和 summary 都说明改了 `app.py` 且检查通过 |
| 22 | `test_openai_multiturn_protocol` | 真实协议上的多轮，而不是只读一次文件 | 六次请求按顺序带上 glob、文件内容、失败、成功；结束后检查能过 |
| 23 | `test_anthropic_multiturn_protocol` | 同上，Anthropic `tool_use` | 与 22 相同 |
| 24 | `test_continues_after_ambiguous_edit` | 补丁失败后换一种改法，进程不退出 | 歧义编辑不改文件；下一刀成功；trace 含 `ambiguous`；退出码 0 |
| 25 | `test_compaction_keeps_agents_md` | 上下文满了以后项目指令还在 | 最后一次请求含 `RULE_ALWAYS_ADD_HEADER`、`[compacted]` 和最近的 `short-mark`，不含 80 个连续的 Q |
| 26 | `test_resume_appends_user_message` | 同一会话里追加一句，改变后续动作 | 第二次请求同时含原任务、上一句答复和新指令；不存在的 session 退出码 1 |
| 27 | `test_default_permission_does_not_hang` | 非交互时不要干等批准 | 不传 `--permission` 也在限时内结束；bash 没跑；trace 含 `permission denied` |

`--decider FILE` 每轮执行 `python FILE`。stdin 是 `{"turn": N, "messages": [...]}`，stdout 是一个动作：`{"type":"tool","name","input"}` 或 `{"type":"message","text"}`。`messages` 里的工具结果是 `{"role":"tool","name","content"}`。bash 的 `content` 含 stdout，并含字面量 `exit_code=0` 或 `exit_code=1`。

`--resume` 接着同一个 `--session-id`。会话 jsonl 里要能找到上一轮的用户原文和模型最终答复。新的 `-p` 出现在恢复后的第一次模型请求里。

仍不做：Tab 补全、向量索引、浏览器点击、云端虚拟机、多代理界面。

假模型脚本格式：

```json
{"turns": [
  {"type": "tool", "name": "edit_file", "input": {"path": "app.py", "old": "a", "new": "b"}},
  {"type": "message", "text": "done"}
]}
```
