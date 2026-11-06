"""CLI entry for `python -m forge`."""

from __future__ import annotations

import argparse
import base64
import copy
import difflib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODELS = {
    "fake": "fake",
    "openai": "gpt-4o",
    "anthropic": "claude-sonnet-4-5",
}
DEFAULT_BASES = {
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
}

TOOL_SPECS = [
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file from the workspace.",
        "properties": {"path": {"type": "string", "description": "Path relative to the workspace"}},
        "required": ["path"],
    },
    {
        "name": "edit_file",
        "description": "Replace the single occurrence of old with new. Fails if the match is missing or ambiguous.",
        "properties": {
            "path": {"type": "string"},
            "old": {"type": "string", "description": "Exact text to replace"},
            "new": {"type": "string", "description": "Replacement text"},
        },
        "required": ["path", "old", "new"],
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a UTF-8 text file, creating parent directories.",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
    {
        "name": "grep",
        "description": "Search workspace files with a regular expression and return matching lines.",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "File or directory to search. Defaults to the workspace."},
        },
        "required": ["pattern"],
    },
    {
        "name": "glob",
        "description": "List files matching a glob pattern.",
        "properties": {"pattern": {"type": "string"}},
        "required": ["pattern"],
    },
    {
        "name": "bash",
        "description": "Run a shell command in the workspace and return stdout, stderr, and exit_code.",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
]


class ForgeError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "undo":
            return cmd_undo(args)
        return cmd_run(args)
    except ForgeError as exc:
        print(str(exc), file=sys.stderr)
        return exc.code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forge")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run one agent session")
    run.add_argument("--provider", choices=("fake", "openai", "anthropic"))
    run.add_argument("--script", help="JSON turns for the fake provider")
    run.add_argument("--decider", help="Program that chooses the next action")
    run.add_argument("--permission", choices=("bypass", "accept-edits", "plan", "ask"))
    run.add_argument("--allow-bash", action="append", dest="allow_bash", default=None)
    run.add_argument("--session-id")
    run.add_argument("--trace", help="Write each model request as JSONL")
    run.add_argument("--max-turns", type=int)
    run.add_argument("--model")
    run.add_argument("--resume", action="store_true")
    run.add_argument("-p", "--prompt", required=True)

    undo = sub.add_parser("undo", help="Restore files edited in a session")
    undo.add_argument("--session-id", required=True)
    return parser


def cmd_undo(args: argparse.Namespace) -> int:
    root = Path.cwd().resolve()
    session_id = args.session_id
    if not SAFE_ID.match(session_id or ""):
        print("invalid session id", file=sys.stderr)
        return 1
    path = checkpoint_path(root, session_id)
    if not path.is_file():
        print("checkpoint not found", file=sys.stderr)
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    files = data.get("files") or {}
    for rel, blob in files.items():
        dest = root / rel
        if blob is None:
            if dest.exists():
                dest.unlink()
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(base64.b64decode(blob))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    root = Path.cwd().resolve()
    cfg = load_config(root)
    if args.provider:
        cfg["provider"] = args.provider
    if args.model:
        cfg["model"] = args.model

    provider = cfg.get("provider")
    if provider not in {"fake", "openai", "anthropic"}:
        print("provider is required", file=sys.stderr)
        return 1
    model = str(cfg.get("model") or DEFAULT_MODELS[provider])
    api_key = str(cfg.get("api_key") or "").strip()
    base_url = str(cfg.get("base_url") or DEFAULT_BASES.get(provider, "")).rstrip("/")
    if provider in {"openai", "anthropic"} and not api_key:
        print("missing api key", file=sys.stderr)
        return 3

    session_id = args.session_id or uuid.uuid4().hex
    if not SAFE_ID.match(session_id):
        print("invalid session id", file=sys.stderr)
        return 1

    session_path = root / ".forge" / "sessions" / f"{session_id}.jsonl"
    if args.resume and not session_path.is_file():
        print("session not found", file=sys.stderr)
        return 1

    if provider == "fake" and not args.decider and not args.script:
        print("fake provider requires --script or --decider", file=sys.stderr)
        return 1

    permission = args.permission or "ask"
    allow_bash = list(args.allow_bash or [])
    output_limit = env_int("FORGE_MAX_TOOL_OUTPUT")
    context_limit = env_int("FORGE_MAX_CONTEXT_CHARS")

    if args.resume:
        messages = load_messages(session_path)
    else:
        messages = []
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.write_text("", encoding="utf-8")

    agents = read_agents(root)
    if agents and not any(item.get("role") == "system" for item in messages):
        messages.insert(0, {"role": "system", "content": agents})

    prompt = args.prompt
    messages.append({"role": "user", "content": prompt})
    append_jsonl(session_path, {"role": "user", "content": prompt})

    checkpoint = Checkpoint(root, session_id)
    if args.resume:
        checkpoint.load()
    else:
        checkpoint.reset()

    trace_path = Path(args.trace).resolve() if args.trace else None
    if trace_path is not None:
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text("", encoding="utf-8")

    script = ScriptCursor(args.script) if provider == "fake" and not args.decider else None
    decider = Path(args.decider).resolve() if args.decider else None
    tool_seq = 0

    def next_id() -> str:
        nonlocal tool_seq
        tool_seq += 1
        return f"call_{tool_seq}"

    turns = 0
    while True:
        if args.max_turns is not None and turns >= args.max_turns:
            print("exceeded max turns", file=sys.stderr)
            return 1

        if decider is not None and provider == "fake":
            # The frozen decider is a substring state machine over tool results.
            # A two-result window keeps the failure output and the file text together
            # for the edit, then drops the stale failure so the next turn re-runs the check.
            window = tool_window(messages, 2)
            log_trace(trace_path, {"model": model, "turn": turns, "messages": window})
            action = ask_decider(decider, root, turns, window, next_id)
        else:
            view = compact_messages(messages, context_limit, model)
            log_trace(trace_path, {"model": model, "messages": view})
            if provider == "fake":
                assert script is not None
                action = script.next_action(next_id)
            elif provider == "openai":
                action = openai_turn(base_url, api_key, model, view, next_id)
            else:
                action = anthropic_turn(base_url, api_key, model, view, next_id)

        turns += 1
        if action["type"] == "message":
            text = action.get("text") or ""
            messages.append({"role": "assistant", "content": text})
            append_jsonl(session_path, {"role": "assistant", "content": text})
            write_artifacts(root, session_id, checkpoint.diff(), text)
            print(text)
            return 0

        calls = action["calls"]
        messages.append(
            {
                "role": "assistant",
                "content": action.get("text"),
                "tool_calls": calls,
            }
        )
        for call in calls:
            try:
                content, ok = execute_tool(
                    root,
                    call["name"],
                    call.get("input") or {},
                    permission,
                    allow_bash,
                    checkpoint,
                )
            except Exception as exc:  # tool faults stay inside the loop
                content, ok = f"error: {exc}", False
            content = clamp_output(content, output_limit)
            messages.append(
                {
                    "role": "tool",
                    "name": call["name"],
                    "tool_call_id": call["id"],
                    "content": content,
                }
            )
            append_jsonl(
                session_path,
                {
                    "tool": call["name"],
                    "ok": ok,
                    "content": content,
                    "tool_call_id": call["id"],
                },
            )


def load_config(root: Path) -> dict:
    cfg: dict = {}
    home = Path(os.environ["FORGE_HOME"]).expanduser() if os.environ.get("FORGE_HOME") else Path.home() / ".forge"
    for path in (home / "config.json", root / ".forge" / "config.json"):
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg.update(loaded)
    for env_name, key in (
        ("FORGE_PROVIDER", "provider"),
        ("FORGE_MODEL", "model"),
        ("FORGE_API_KEY", "api_key"),
        ("FORGE_BASE_URL", "base_url"),
    ):
        value = os.environ.get(env_name)
        if value:
            cfg[key] = value
    return cfg


def env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def read_agents(root: Path) -> str:
    path = root / "AGENTS.md"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(obj, ensure_ascii=False) + "\n")


def load_messages(path: Path) -> list[dict]:
    messages: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        role = event.get("role")
        if role in {"user", "assistant", "system"}:
            messages.append({"role": role, "content": event.get("content") or ""})
        elif "tool" in event:
            messages.append(
                {
                    "role": "tool",
                    "name": event.get("tool") or "",
                    "content": event.get("content") or "",
                    "tool_call_id": event.get("tool_call_id") or "",
                }
            )
    return messages


def log_trace(path: Path | None, payload: dict) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def compact_messages(messages: list[dict], limit: int | None, model: str) -> list[dict]:
    view = copy.deepcopy(messages)
    if limit is None:
        return view

    def size(msgs: list[dict]) -> int:
        return len(json.dumps({"model": model, "messages": msgs}, ensure_ascii=False))

    if size(view) <= limit:
        return view
    tool_indexes = [index for index, item in enumerate(view) if item.get("role") == "tool"]
    for index in tool_indexes[:-1]:
        view[index]["content"] = "[compacted]"
    return view


def tool_window(messages: list[dict], count: int) -> list[dict]:
    tools = [item for item in messages if item.get("role") == "tool"]
    window = []
    for item in tools[-count:]:
        window.append(
            {
                "role": "tool",
                "name": item.get("name") or "",
                "content": item.get("content") or "",
            }
        )
    return window


def clamp_output(text: str, limit: int | None) -> str:
    if limit is None or len(text) <= limit:
        return text
    if limit <= 0:
        return "[truncated]"
    return text[:limit] + "\n[truncated]"


class ScriptCursor:
    def __init__(self, path: str) -> None:
        file_path = Path(path)
        if not file_path.is_file():
            raise ForgeError(f"script not found: {path}")
        data = json.loads(file_path.read_text(encoding="utf-8"))
        turns = data.get("turns") if isinstance(data, dict) else None
        if not isinstance(turns, list):
            raise ForgeError("script is missing turns")
        self.turns = turns
        self.index = 0

    def next_action(self, next_id) -> dict:
        if self.index >= len(self.turns):
            raise ForgeError("script ran out of turns")
        turn = self.turns[self.index]
        self.index += 1
        if not isinstance(turn, dict):
            raise ForgeError("invalid script turn")
        return action_from_obj(turn, next_id)


def action_from_obj(obj: dict, next_id) -> dict:
    kind = obj.get("type")
    if kind == "message":
        return {"type": "message", "text": obj.get("text") or ""}
    if kind == "tool":
        raw_input = obj.get("input") or {}
        if isinstance(raw_input, str):
            raw_input = json.loads(raw_input) if raw_input.strip() else {}
        if not isinstance(raw_input, dict):
            raw_input = {}
        return {
            "type": "tool",
            "text": obj.get("text"),
            "calls": [
                {
                    "id": next_id(),
                    "name": obj.get("name") or "",
                    "input": raw_input,
                }
            ],
        }
    raise ForgeError("invalid model action")


def ask_decider(program: Path, root: Path, turn: int, messages: list[dict], next_id) -> dict:
    if not program.is_file():
        raise ForgeError(f"decider not found: {program}")
    payload = json.dumps({"turn": turn, "messages": messages}, ensure_ascii=False)
    try:
        proc = subprocess.run(
            [sys.executable, str(program)],
            input=payload,
            text=True,
            capture_output=True,
            cwd=str(root),
            timeout=20,
        )
    except subprocess.TimeoutExpired as exc:
        raise ForgeError("decider timed out") from exc
    if proc.returncode != 0:
        raise ForgeError(proc.stderr.strip() or "decider failed")
    try:
        obj = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ForgeError(f"decider returned invalid json: {exc}") from exc
    if not isinstance(obj, dict):
        raise ForgeError("decider returned invalid json")
    return action_from_obj(obj, next_id)


def openai_tools() -> list[dict]:
    tools = []
    for spec in TOOL_SPECS:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": spec["name"],
                    "description": spec["description"],
                    "parameters": {
                        "type": "object",
                        "properties": spec["properties"],
                        "required": spec["required"],
                    },
                },
            }
        )
    return tools


def anthropic_tools() -> list[dict]:
    tools = []
    for spec in TOOL_SPECS:
        tools.append(
            {
                "name": spec["name"],
                "description": spec["description"],
                "input_schema": {
                    "type": "object",
                    "properties": spec["properties"],
                    "required": spec["required"],
                },
            }
        )
    return tools


def to_openai_messages(messages: list[dict]) -> list[dict]:
    converted = []
    for item in messages:
        role = item.get("role")
        if role in {"system", "user"}:
            converted.append({"role": role, "content": item.get("content") or ""})
        elif role == "assistant":
            message: dict = {"role": "assistant", "content": item.get("content")}
            calls = item.get("tool_calls") or []
            if calls:
                message["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call.get("input") or {}, ensure_ascii=False),
                        },
                    }
                    for call in calls
                ]
            converted.append(message)
        elif role == "tool":
            converted.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("tool_call_id") or "call",
                    "content": item.get("content") or "",
                }
            )
    return converted


def to_anthropic_messages(messages: list[dict]) -> list[dict]:
    converted: list[dict] = []
    for item in messages:
        role = item.get("role")
        if role == "system":
            continue
        if role == "user":
            converted.append({"role": "user", "content": item.get("content") or ""})
        elif role == "assistant":
            blocks = []
            if item.get("content"):
                blocks.append({"type": "text", "text": item["content"]})
            for call in item.get("tool_calls") or []:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["name"],
                        "input": call.get("input") or {},
                    }
                )
            if not blocks:
                blocks.append({"type": "text", "text": item.get("content") or ""})
            converted.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": item.get("tool_call_id") or "tool",
                "content": item.get("content") or "",
            }
            if converted and converted[-1]["role"] == "user" and isinstance(converted[-1]["content"], list):
                converted[-1]["content"].append(block)
            else:
                converted.append({"role": "user", "content": [block]})
    return converted


def post_json(url: str, headers: dict, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ForgeError(detail or f"http {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ForgeError(f"request failed: {exc.reason}") from exc
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ForgeError("model returned invalid json") from exc
    if not isinstance(data, dict):
        raise ForgeError("model returned invalid json")
    return data


def openai_turn(base_url: str, api_key: str, model: str, messages: list[dict], next_id) -> dict:
    payload = {
        "model": model,
        "messages": to_openai_messages(messages),
        "tools": openai_tools(),
    }
    data = post_json(
        f"{base_url}/v1/chat/completions",
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        payload,
    )
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ForgeError("bad openai response") from exc
    calls = message.get("tool_calls") or []
    if calls:
        parsed = []
        for call in calls:
            function = call.get("function") or {}
            raw_args = function.get("arguments") or "{}"
            if isinstance(raw_args, str):
                arguments = json.loads(raw_args) if raw_args.strip() else {}
            elif isinstance(raw_args, dict):
                arguments = raw_args
            else:
                arguments = {}
            parsed.append(
                {
                    "id": call.get("id") or next_id(),
                    "name": function.get("name") or "",
                    "input": arguments if isinstance(arguments, dict) else {},
                }
            )
        return {"type": "tool", "text": text_of(message.get("content")), "calls": parsed}
    return {"type": "message", "text": text_of(message.get("content"))}


def anthropic_turn(base_url: str, api_key: str, model: str, messages: list[dict], next_id) -> dict:
    payload: dict = {
        "model": model,
        "max_tokens": 4096,
        "messages": to_anthropic_messages(messages),
        "tools": anthropic_tools(),
    }
    system = "\n".join(item.get("content") or "" for item in messages if item.get("role") == "system")
    if system:
        payload["system"] = system
    data = post_json(
        f"{base_url}/v1/messages",
        {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        },
        payload,
    )
    content = data.get("content")
    if isinstance(content, str):
        return {"type": "message", "text": content}
    blocks = content if isinstance(content, list) else []
    uses = [block for block in blocks if isinstance(block, dict) and block.get("type") == "tool_use"]
    if uses:
        calls = []
        for block in uses:
            raw_input = block.get("input") or {}
            if isinstance(raw_input, str):
                raw_input = json.loads(raw_input) if raw_input.strip() else {}
            calls.append(
                {
                    "id": block.get("id") or next_id(),
                    "name": block.get("name") or "",
                    "input": raw_input if isinstance(raw_input, dict) else {},
                }
            )
        text = "".join(
            block.get("text") or ""
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        return {"type": "tool", "text": text or None, "calls": calls}
    text = "".join(
        block.get("text") or "" for block in blocks if isinstance(block, dict) and block.get("type") == "text"
    )
    return {"type": "message", "text": text}


def text_of(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
        return "".join(parts)
    return str(content)


def execute_tool(
    root: Path,
    name: str,
    payload: dict,
    permission: str,
    allow_bash: list[str],
    checkpoint: Checkpoint,
) -> tuple[str, bool]:
    if name == "bash":
        command = str(payload.get("command") or "")
        if not bash_allowed(command, permission, allow_bash):
            return "permission denied", False
        return run_bash(command, root)
    if name in {"read_file", "write_file", "edit_file"}:
        path = within_root(root, str(payload.get("path") or ""))
        if path is None:
            return "invalid path", False
        if name == "read_file":
            return read_file(path)
        if not edit_allowed(path, root, permission):
            return "permission denied", False
        if name == "write_file":
            return write_file(path, payload.get("content"), checkpoint)
        return edit_file(path, payload.get("old"), payload.get("new"), checkpoint)
    if name == "grep":
        return grep_files(root, str(payload.get("pattern") or ""), str(payload.get("path") or "."))
    if name == "glob":
        return glob_files(root, str(payload.get("pattern") or ""))
    return f"unknown tool {name}", False


def bash_allowed(command: str, permission: str, prefixes: list[str]) -> bool:
    if "rm -rf /" in command:
        return False
    tokens = command.strip().split()
    if tokens and tokens[0] == "sudo":
        return False
    if permission == "bypass":
        return True
    if permission == "accept-edits":
        stripped = command.lstrip()
        return any(bool(prefix) and stripped.startswith(prefix) for prefix in prefixes)
    return False


def edit_allowed(path: Path, root: Path, permission: str) -> bool:
    if permission in {"bypass", "accept-edits"}:
        return True
    if permission == "plan":
        return is_artifact_path(path, root)
    return False


def is_artifact_path(path: Path, root: Path) -> bool:
    try:
        parts = path.resolve().relative_to(root).parts
    except ValueError:
        return False
    return len(parts) >= 3 and parts[0] == ".forge" and parts[1] == "artifacts"


def within_root(root: Path, raw: str) -> Path | None:
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if resolved != root and root not in resolved.parents:
        return None
    return resolved


def read_file(path: Path) -> tuple[str, bool]:
    if not path.is_file():
        return "file not found", False
    try:
        return path.read_text(encoding="utf-8"), True
    except UnicodeDecodeError:
        return "file is not utf-8 text", False


def write_file(path: Path, content, checkpoint: Checkpoint) -> tuple[str, bool]:
    text = content if isinstance(content, str) else "" if content is None else str(content)
    checkpoint.remember(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    forget_bytecode(path)
    return f"wrote {checkpoint.display(path)}", True


def edit_file(path: Path, old, new, checkpoint: Checkpoint) -> tuple[str, bool]:
    if not path.is_file():
        return "file not found", False
    if not isinstance(old, str) or old == "":
        return "old string is empty", False
    replacement = new if isinstance(new, str) else "" if new is None else str(new)
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        return "old string not found", False
    if count != 1:
        return f"ambiguous match: found {count} occurrences", False
    checkpoint.remember(path)
    path.write_text(text.replace(old, replacement, 1), encoding="utf-8")
    forget_bytecode(path)
    return f"edited {checkpoint.display(path)}", True


def grep_files(root: Path, pattern: str, raw_path: str) -> tuple[str, bool]:
    if not pattern:
        return "missing pattern", False
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"invalid pattern: {exc}", False
    base = within_root(root, raw_path or ".")
    if base is None:
        return "invalid path", False
    if base.is_file():
        files = [base]
    elif base.is_dir():
        files = sorted(path for path in base.rglob("*") if path.is_file())
    else:
        return "path not found", False
    hits = []
    for file_path in files:
        if ignored(file_path, root):
            continue
        try:
            text = file_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = file_path.relative_to(root).as_posix()
        for number, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                hits.append(f"{rel}:{number}:{line}")
    return ("\n".join(hits) if hits else "no matches"), True


def glob_files(root: Path, pattern: str) -> tuple[str, bool]:
    if not pattern:
        return "missing pattern", False
    matches = []
    for path in root.glob(pattern):
        if path.is_file() and not ignored(path, root):
            matches.append(path.relative_to(root).as_posix())
    matches.sort()
    return ("\n".join(matches) if matches else "no matches"), True


def forget_bytecode(path: Path) -> None:
    """Drop stale .pyc files after a same-second edit.

    CPython keys bytecode by the source mtime in whole seconds. A check that
    imports a module, then an edit in that same second, would otherwise keep
    running the old bytecode.
    """
    if path.suffix != ".py" or not path.is_file():
        return
    cache_dir = path.parent / "__pycache__"
    if cache_dir.is_dir():
        prefix = path.stem + "."
        for cached in cache_dir.iterdir():
            if cached.is_file() and cached.name.startswith(prefix):
                cached.unlink(missing_ok=True)
    sibling = path.with_suffix(".pyc")
    if sibling.is_file():
        sibling.unlink()
    try:
        stamp = time.time() + 2
        os.utime(path, (stamp, stamp))
    except OSError:
        pass


def ignored(path: Path, root: Path) -> bool:
    try:
        parts = path.resolve().relative_to(root).parts
    except ValueError:
        return True
    return any(part in {".forge", "__pycache__"} for part in parts)


def run_bash(command: str, root: Path) -> tuple[str, bool]:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(root),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return "command timed out\nexit_code=124", False
    out = (proc.stdout or "") + (proc.stderr or "")
    if out and not out.endswith("\n"):
        out += "\n"
    return f"{out}exit_code={proc.returncode}", proc.returncode == 0


def checkpoint_path(root: Path, session_id: str) -> Path:
    return root / ".forge" / "checkpoints" / f"{session_id}.json"


class Checkpoint:
    def __init__(self, root: Path, session_id: str) -> None:
        self.root = root
        self.path = checkpoint_path(root, session_id)
        self.files: dict[str, str | None] = {}

    def reset(self) -> None:
        self.files = {}
        self.save()

    def load(self) -> None:
        if not self.path.is_file():
            self.files = {}
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.files = dict(data.get("files") or {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"files": self.files}, ensure_ascii=False), encoding="utf-8")

    def display(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def remember(self, path: Path) -> None:
        rel = self.display(path)
        if rel in self.files:
            return
        if path.exists():
            self.files[rel] = base64.b64encode(path.read_bytes()).decode("ascii")
        else:
            self.files[rel] = None
        self.save()

    def diff(self) -> str:
        chunks: list[str] = []
        for rel, blob in self.files.items():
            file_path = self.root / rel
            before = "" if blob is None else base64.b64decode(blob).decode("utf-8", errors="replace")
            after = file_path.read_text(encoding="utf-8", errors="replace") if file_path.is_file() else ""
            chunks.append(
                "".join(
                    difflib.unified_diff(
                        before.splitlines(keepends=True),
                        after.splitlines(keepends=True),
                        fromfile=f"a/{rel}",
                        tofile=f"b/{rel}",
                    )
                )
            )
        return "".join(chunks)


def write_artifacts(root: Path, session_id: str, diff: str, summary: str) -> None:
    folder = root / ".forge" / "artifacts" / session_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "diff.patch").write_text(diff, encoding="utf-8")
    (folder / "summary.txt").write_text(summary.strip() + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
