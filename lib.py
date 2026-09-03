"""Shared plumbing: credentials, agent files, and the AssemblyAI Agents API.

Copied from the AssemblyAI Voice Agent starter so the two repos publish agents
the same way: https://github.com/AssemblyAI/voice-agent-starter-python

Standard library only.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
AGENT_DIR = ROOT / "agents"


# --- environment ------------------------------------------------------------


def load_env(path: Path = ENV_FILE) -> None:
    """KEY=value per line, # for comments, quotes optional. Anything already in
    the environment wins, so hosting platforms and shell overrides take
    precedence over the file."""
    try:
        text = path.read_text()
    except OSError:
        return
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$", line)
        if not match:
            continue
        key, raw = match.group(1), match.group(2)
        if key in os.environ:
            continue
        os.environ[key] = re.sub(r"^(['\"])(.*)\1$", r"\2", raw)


def save_env(key: str, value: str, path: Path = ENV_FILE) -> bool:
    """Write a key back to .env, in place if it is already there. Hosting
    platforms have no writable .env, so failure is reported, not fatal."""
    os.environ[key] = value
    try:
        text = path.read_text()
    except OSError:
        text = ""
    line = f"{key}={value}"
    pattern = re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*=.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(line, text, count=1)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += line + "\n"
    try:
        path.write_text(text)
        return True
    except OSError:
        return False


def required(name: str, hint: str = "") -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Missing {name}" + (f". {hint}" if hint else ""))
    return value


# --- agent files ------------------------------------------------------------


def list_agents() -> list[str]:
    return sorted(p.stem for p in AGENT_DIR.glob("*.jsonc"))


def parse_jsonc(text: str) -> Any:
    """The agent files are JSON with comments, so every field can carry a note
    and a link to the docs page that defines it. Comments and trailing commas
    are stripped here; what reaches the API is plain JSON."""
    out: list[str] = []
    in_string = escaped = in_line_comment = in_block_comment = False
    i = 0
    while i < len(text):
        char = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if in_line_comment:
            if char == "\n":
                in_line_comment = False
                out.append(char)
            i += 1
            continue
        if in_block_comment:
            if char == "*" and nxt == "/":
                in_block_comment = False
                i += 1
            i += 1
            continue
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            i += 1
            continue
        if char == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if char == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        # A comma left dangling by a commented-out field would break the parse.
        if char in "}]":
            while out and out[-1].isspace():
                out.pop()
            if out and out[-1] == ",":
                out.pop()
        out.append(char)
        i += 1
    return json.loads("".join(out))


def _interpolate(value: Any, missing: set) -> Any:
    if isinstance(value, str):
        def swap(match: re.Match) -> str:
            name = match.group(1)
            if not os.environ.get(name):
                missing.add(name)
                return match.group(0)
            return os.environ[name]

        return re.sub(r"\$\{([A-Za-z0-9_]+)\}", swap, value)
    if isinstance(value, list):
        return [_interpolate(item, missing) for item in value]
    if isinstance(value, dict):
        return {key: _interpolate(item, missing) for key, item in value.items()}
    return value


def read_agent(name: str) -> dict:
    """An agent file is the request body for POST /v1/agents, nothing more."""
    path = AGENT_DIR / f"{name}.jsonc"
    if not path.exists():
        sys.exit(f"No agents/{name}.jsonc. Set AGENT to one of: {', '.join(list_agents())}")
    # Keys shared by everything live in the root .env; keys only this agent
    # needs can live beside it in agents/<name>.env, gitignored the same way.
    load_env(AGENT_DIR / f"{name}.env")
    missing: set = set()
    agent = _interpolate(parse_jsonc(path.read_text()), missing)
    if missing:
        names = ", ".join(sorted(missing))
        sys.exit(f"agents/{name}.jsonc needs {names}. Add "
                 + ("them" if len(missing) > 1 else "it") + " to .env")
    return agent


# --- AssemblyAI -------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, label: str, status: int, body: str):
        super().__init__(f"{label} failed ({status}): {body}")
        self.status = status


def _agents_api() -> str:
    # The API also answers on regional hosts; set AGENTS_API_BASE if the
    # account is pinned to one.
    return os.environ.get("AGENTS_API_BASE", "https://agents.assemblyai.com/v1")


def _request(url: str, label: str, method: str, headers: dict, data: Optional[bytes]) -> str:
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as res:
            return res.read().decode()
    except urllib.error.HTTPError as err:
        raise ApiError(label, err.code, err.read().decode()) from None


def aai(path: str, method: str = "GET", body: Any = None, headers: Optional[dict] = None) -> Any:
    request_headers = {
        "Authorization": f"Bearer {os.environ.get('ASSEMBLYAI_API_KEY', '')}",
        "Content-Type": "application/json",
        **(headers or {}),
    }
    data = json.dumps(body).encode() if body is not None else None
    text = _request(_agents_api() + path, f"{method} {path}", method, request_headers, data)
    try:
        return json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {}


def agent_id_key(name: str) -> str:
    return "AGENT_ID_" + re.sub(r"[^A-Z0-9]", "_", name.upper())


def stored_agent_id(name: str) -> str:
    """Each agent file gets its own id, so switching files does not overwrite
    the agent the last one published. A bare AGENT_ID overrides them all, for
    an agent shaped in the dashboard or a hosted deploy."""
    return os.environ.get("AGENT_ID") or os.environ.get(agent_id_key(name), "")


def publish_agent(agent: dict, name: str = "", reuse_by_name: bool = False) -> dict:
    """An id in the environment decides create versus update. Absent, POST a
    new agent and remember the id. Present, PUT the file over that agent."""
    key = agent_id_key(name)
    explicit = bool(os.environ.get("AGENT_ID"))
    agent_id = stored_agent_id(name)
    if agent_id:
        try:
            current = aai(f"/agents/{agent_id}")
            if current.get("name") and current["name"] != agent.get("name"):
                print(f'Note: agent {agent_id} was "{current["name"]}"')
            aai(f"/agents/{agent_id}", method="PUT", body=agent)
            return {"id": agent_id, "created": False, "saved": True, "key": key}
        except ApiError as err:
            if err.status != 404:
                raise
            print(f"Agent {agent_id} no longer exists, creating a new one")
    # A hosted server has no AGENT_ID and no writable .env, so without this it
    # would POST another agent on every restart.
    if reuse_by_name:
        existing = next(
            (a for a in aai("/agents").get("agents", []) if a.get("name") == agent.get("name")),
            None,
        )
        if existing:
            aai(f"/agents/{existing['id']}", method="PUT", body=agent)
            return {"id": existing["id"], "created": False,
                    "saved": save_env(key, existing["id"]), "key": key}
    created = aai("/agents", method="POST", body=agent)
    # An explicit AGENT_ID is the caller's choice, so it is not overwritten.
    saved = False if explicit else save_env(key, created["id"])
    return {"id": created["id"], "created": True, "saved": saved, "key": key}
