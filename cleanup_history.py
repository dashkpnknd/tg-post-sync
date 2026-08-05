import argparse
import asyncio
import json
import os
from pathlib import Path

from sync_engine import cleanup_plan, cleanup_untouched, make_client


async def main(task_name: str, dry_run: bool):
    data = json.loads(Path("data.json").read_text())
    task = next((item for item in data["tasks"].values() if item["name"] == task_name), None)
    if not task:
        raise SystemExit(f"Task not found: {task_name}")
    account = data["accounts"][task["account_id"]]
    client = make_client(int(os.environ["API_ID"]), os.environ["API_HASH"], account["session"])
    await client.connect()
    try:
        if dry_run:
            candidates, skipped, copied = await cleanup_plan(
                client, task["source"], task["target"], task["history_count"], task.get("target_skip", 0)
            )
            print(json.dumps({"would_delete": len(candidates), "skipped": skipped, "copied": copied}, ensure_ascii=False))
            return
        deleted, skipped, copied = await cleanup_untouched(client, task["source"], task["target"], task["history_count"], task.get("target_skip", 0))
    finally:
        await client.disconnect()
    print(json.dumps({"deleted": deleted, "skipped": skipped, "copied": copied}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("task_name")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.task_name, args.dry_run))
