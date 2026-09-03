#!/usr/bin/env python3
"""Push agents/<name>.jsonc to your AssemblyAI account.

    python publish.py
    AGENT=<name> python publish.py

The first run creates the agent and writes AGENT_ID_<NAME> to .env. Later runs
update that same agent, so each file keeps its own agent and the next
simulation picks up the change.
"""

import os
import sys

from lib import ApiError, list_agents, load_env, publish_agent, read_agent, required

NOTHING_TO_PUBLISH = """No agent files yet. agents/ starts empty: the agent under test is yours.

  python import_agent.py <agent-id>     # pull in one you already have

or write agents/<name>.jsonc yourself, the body of POST /v1/agents. See agents/README.md."""


def main() -> None:
    load_env()
    required("ASSEMBLYAI_API_KEY", "get one at https://www.assemblyai.com/dashboard/api-keys")

    available = list_agents()
    if not available:
        sys.exit(NOTHING_TO_PUBLISH)
    name = os.environ.get("AGENT") or available[0]
    agent = read_agent(name)
    result = publish_agent(agent, name=name)

    verb = "Created" if result["created"] else "Updated"
    print(f'{verb} "{agent["name"]}" from agents/{name}.jsonc')
    print(f"{result['key']}={result['id']}")
    if result["created"]:
        print("Saved to .env." if result["saved"]
              else f"Could not write .env. Set {result['key']} yourself to keep updating this agent.")

    # Tools with an http block are called by AssemblyAI, so they work during a
    # simulation with nothing to wire up. Anything else has to be answered by
    # whoever holds the session, and the bridge is transport only.
    unanswered = [tool["name"] for tool in agent.get("tools", []) if not tool.get("http")]
    if unanswered:
        have = "have" if len(unanswered) > 1 else "has"
        print(f"\nWarning: {', '.join(unanswered)} {have} no http block, "
              "so nothing answers it during a simulation.")


if __name__ == "__main__":
    try:
        main()
    except ApiError as err:
        sys.exit(str(err))
