#!/usr/bin/env python3
"""Pull an agent you already have into this repo as a file.

    python import_agent.py 8f3c1e2a-...
    AGENT_ID=8f3c1e2a-... python import_agent.py

Fetches the agent, writes agents/<name>.jsonc from it, and records the id in
.env so `python publish.py` updates that agent rather than creating another.
This is the path from an agent you already run in production into a file
Bluejay can be pointed at.
"""

import json
import os
import re
import sys

from lib import AGENT_DIR, ApiError, aai, agent_id_key, load_env, required, save_env

HEADER = """// Imported from agent {id}.
//
// This is the body of POST /v1/agents, so every field is documented at
// https://www.assemblyai.com/docs/voice-agents/voice-agent-api/create-agent
// Edit it and run `python publish.py` to push the change back to the same agent.
"""


def main() -> None:
    load_env()
    required("ASSEMBLYAI_API_KEY", "get one at https://www.assemblyai.com/dashboard/api-keys")

    agent_id = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("AGENT_ID")
    if not agent_id:
        print("Usage: python import_agent.py <agent-id>", file=sys.stderr)
        sys.exit("The id is in the URL of the agent in the AssemblyAI dashboard.")

    agent = aai(f"/agents/{agent_id}")

    # Fields the API fills in are not part of a create request, so they would
    # be noise in the file.
    body = {k: v for k, v in agent.items() if k not in ("id", "created_at", "updated_at")}

    slug = os.environ.get("AGENT") or re.sub(
        r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", (body.get("name") or "").lower())
    ) or "imported-agent"

    path = AGENT_DIR / f"{slug}.jsonc"
    if path.exists() and not os.environ.get("OVERWRITE"):
        sys.exit(f"agents/{slug}.jsonc already exists. Set AGENT=<other-name> or OVERWRITE=1.")

    path.write_text(HEADER.format(id=agent_id) + json.dumps(body, indent=2) + "\n")
    key = agent_id_key(slug)
    saved = save_env(key, agent_id)

    print(f'Wrote agents/{slug}.jsonc from "{body.get("name")}"')
    print(f"Saved {key} to .env." if saved
          else f"Could not write .env. Set {key}={agent_id} yourself.")
    print(f"\n  AGENT={slug} python bridge.py")

    # Credentials are write-only on the API, so they cannot come back with the
    # agent. Anything that needs one has to be filled in again, as a ${VAR}.
    blanked = [t["name"] for t in body.get("tools", [])
               if any(not h.get("value") for h in t.get("http", {}).get("headers", []))]
    if blanked:
        print(f"\nHeader values are write-only and did not come back for: {', '.join(blanked)}. "
              "Put them in .env and reference them as ${VARS}.")
    if body.get("llm"):
        print("\nThe llm entry came back without its api_key. Add it back as a ${VAR}.")


if __name__ == "__main__":
    try:
        main()
    except ApiError as err:
        sys.exit(str(err))
