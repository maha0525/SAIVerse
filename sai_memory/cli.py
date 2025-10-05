from __future__ import annotations

import argparse
import os
import sys

# Allow running from inside the sai_memory directory as a script
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from dotenv import load_dotenv

from sai_memory.agent import Agent
from sai_memory.config import load_settings


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--thread", required=True)
    p.add_argument("--resource", default=os.getenv("SAIMEMORY_RESOURCE_ID", "default"))
    p.add_argument("--input", required=True)
    args = p.parse_args()

    agent = Agent(load_settings())
    out = agent.run(thread_id=args.thread, user_input=args.input, resource_id=args.resource)
    print(out)


if __name__ == "__main__":
    main()
