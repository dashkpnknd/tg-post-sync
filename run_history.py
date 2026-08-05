import argparse
import asyncio
import json
import os
from pathlib import Path

from sync_engine import make_client, migrate_history


async def main(task_name: str):
    data = json.loads(Path("data.json").read_text())
    selected = next((task for task in data["tasks"].values() if task["name"] == task_name), None)
    if not selected:
        raise SystemExit(f"Task not found: {task_name}")
    account = data["accounts"][selected["account_id"]]
    client = make_client(int(os.environ["API_ID"]), os.environ["API_HASH"], account["session"])
    await client.connect()
    try:
        done, total, errors = await migrate_history(
            client, selected["source"], selected["target"], selected["history_count"], selected.get("target_skip", 0)
        )
    finally:
        await client.disconnect()
    print(json.dumps({"done": done, "total": total, "errors": len(errors)}, ensure_ascii=False))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("task_name")
    args = parser.parse_args()
    asyncio.run(main(args.task_name))
