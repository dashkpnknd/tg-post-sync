import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

logger = logging.getLogger(__name__)


def make_client(api_id: int, api_hash: str, session: str) -> TelegramClient:
    return TelegramClient(StringSession(session), api_id, api_hash)


async def copy_message(client: TelegramClient, source, target, message) -> None:
    """Copy a message without source attribution, preserving its media and caption."""
    if message.media:
        await client.send_file(target, message.media, caption=message.message or "")
    elif message.message:
        await client.send_message(target, message.message, link_preview=bool(message.web_preview))


async def newest_posts(client: TelegramClient, peer, limit: int | None):
    """Return newest-first logical posts. An album is represented by its newest item."""
    posts, groups = [], set()
    async for message in client.iter_messages(peer, limit=None if limit == 0 else limit):
        if message.action:
            continue
        if message.grouped_id:
            if message.grouped_id in groups:
                continue
            groups.add(message.grouped_id)
        posts.append(message)
    return posts


async def migrate_history(
    client: TelegramClient,
    source,
    target,
    count: int,
    progress: Callable[[int, int, str], Awaitable[None]] | None = None,
) -> tuple[int, int, list[str]]:
    """Edit destination placeholders from newest to oldest. Returns copied/skipped/errors."""
    source_posts = await newest_posts(client, source, count)
    target_posts = await newest_posts(client, target, count)
    total = min(len(source_posts), len(target_posts))
    errors: list[str] = []
    done = 0
    for index, (src, dst) in enumerate(zip(source_posts[:total], target_posts[:total]), 1):
        try:
            # Telegram cannot turn one existing message into an album. For an album,
            # replace the newest placeholder with the newest media item and report it.
            if src.grouped_id:
                errors.append(f"Источник #{src.id}: альбом требует ручной проверки")
            await client.edit_message(target, dst.id, src.message or "", file=src.media)
            done += 1
        except FloodWaitError as exc:
            await asyncio.sleep(exc.seconds + 1)
            try:
                await client.edit_message(target, dst.id, src.message or "", file=src.media)
                done += 1
            except Exception as retry_error:
                errors.append(f"#{src.id}: {retry_error}")
        except Exception as exc:
            errors.append(f"#{src.id}: {exc}")
        if progress:
            await progress(index, total, errors[-1] if errors else "")
    return done, total, errors


class LiveSync:
    def __init__(self, task: dict, api_id: int, api_hash: str):
        self.task = task
        self.client = make_client(api_id, api_hash, task["session"])
        self.running = False
        self._handled_groups: set[int] = set()

    async def start(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Telegram-сессия больше не авторизована")
        self.running = True
        self.client.add_event_handler(self._on_message, events.NewMessage(chats=self.task["source_id"]))

    async def stop(self) -> None:
        self.running = False
        with contextlib.suppress(Exception):
            await self.client.disconnect()

    async def _on_message(self, event) -> None:
        message = event.message
        if message.action or not self.running:
            return
        if message.grouped_id:
            if message.grouped_id in self._handled_groups:
                return
            self._handled_groups.add(message.grouped_id)
        try:
            await copy_message(self.client, self.task["source_id"], self.task["target_id"], message)
        except FloodWaitError as exc:
            await asyncio.sleep(exc.seconds + 1)
            await copy_message(self.client, self.task["source_id"], self.task["target_id"], message)
        except Exception:
            logger.exception("Could not copy new message for task %s", self.task.get("name"))
