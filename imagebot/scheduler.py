from __future__ import annotations

import asyncio
import io
import logging
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import discord
from discord.ext import tasks

from .cloud import CloudManager
from .config import Settings
from .database import Database
from .media import PreparedImage, prepare_image
from .models import Category, CloudItem, Source

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DueSlot:
    key: str
    scheduled_for: datetime
    label: str


def due_slots(now: datetime, schedule_times: Iterable[str], catchup_minutes: int) -> list[DueSlot]:
    if now.tzinfo is None:
        raise ValueError("now должен содержать часовой пояс")
    result: list[DueSlot] = []
    for label in schedule_times:
        hour, minute = (int(part) for part in label.split(":", 1))
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delay = now - scheduled
        if timedelta(0) <= delay <= timedelta(minutes=catchup_minutes):
            result.append(DueSlot(f"{scheduled.date().isoformat()}@{label}", scheduled, label))
    return result


def slot_is_effective(slot: DueSlot, effective_from: datetime | None) -> bool:
    return effective_from is None or slot.scheduled_for.astimezone(UTC) >= effective_from


class BroadcastService:
    def __init__(
        self,
        client: discord.Client,
        database: Database,
        clouds: CloudManager,
        settings: Settings,
    ):
        self.client = client
        self.database = database
        self.clouds = clouds
        self.settings = settings
        self._lock = asyncio.Lock()

    def start(self) -> None:
        if not self.scheduler.is_running():
            self.scheduler.start()

    def stop(self) -> None:
        if self.scheduler.is_running():
            self.scheduler.cancel()

    @tasks.loop(seconds=60, reconnect=True)
    async def scheduler(self) -> None:
        await self.run_once()

    @scheduler.before_loop
    async def before_scheduler(self) -> None:
        await self.client.wait_until_ready()

    @scheduler.error
    async def scheduler_error(self, error: BaseException) -> None:
        log.exception("Scheduler loop failed", exc_info=error)

    async def run_once(self, *, now: datetime | None = None) -> None:
        if self._lock.locked():
            return
        async with self._lock:
            for dispatch in await self.database.list_pending_dispatches():
                await self._process_dispatch(dispatch)

            current = now or datetime.now(self.settings.timezone)
            schedule_times, effective_from = await self.database.get_schedule_times(self.settings.schedule_times)
            for slot in due_slots(current, schedule_times, self.settings.catchup_window_minutes):
                if not slot_is_effective(slot, effective_from):
                    continue
                if not await self.database.dispatch_exists(slot.key):
                    await self._create_scheduled_dispatch(slot)

    async def update_schedule(self, values: tuple[str, ...], updated_by: int) -> None:
        async with self._lock:
            await self.database.set_schedule_times(values, updated_by)

    async def _choose(self) -> tuple[Category, list[CloudItem], dict[str, str]]:
        sources = await self.database.list_sources(enabled_only=True)
        if not sources:
            raise RuntimeError("Нет активных источников")
        categories, errors = await self.clouds.discover_eligible(sources, self.settings.images_per_post)
        reserved = await self.database.reserved_item_keys()
        available_categories = [
            Category(
                category.source,
                category.id,
                category.name,
                category.path,
                tuple(item for item in category.images if (category.source.kind, item.id) not in reserved),
            )
            for category in categories
        ]
        available_categories = [
            category for category in available_categories if len(category.images) >= self.settings.images_per_post
        ]
        if not available_categories:
            details = "; ".join(f"{name}: {error}" for name, error in errors.items())
            suffix = f" Ошибки: {details}" if details else ""
            raise RuntimeError(
                f"Нет папок, где есть хотя бы {self.settings.images_per_post} свободных изображения.{suffix}"
            )
        category = secrets.choice(available_categories)
        chosen = secrets.SystemRandom().sample(list(category.images), self.settings.images_per_post)
        return category, chosen, errors

    async def preview(self) -> tuple[Category, list[PreparedImage], dict[str, str]]:
        category, items, errors = await self._choose()
        prepared = await self._download_and_prepare(category.source, items)
        return category, prepared, errors

    async def _create_scheduled_dispatch(self, slot: DueSlot) -> None:
        guilds = await self.database.list_enabled_guilds()
        if not guilds:
            log.info("Slot %s skipped: no configured guild channels", slot.key)
            return
        try:
            category, items, errors = await self._choose()
            if errors:
                log.warning("Some sources were unavailable for %s: %s", slot.key, errors)
            dispatch_id = await self.database.create_dispatch(
                slot_key=slot.key,
                scheduled_for=slot.scheduled_for.isoformat(),
                source_id=category.source.id,
                category_id=category.id,
                category_name=category.name,
                items=items,
                target_guild_ids=list(guilds),
            )
            dispatch = await self.database.get_dispatch(dispatch_id)
            if dispatch:
                await self._process_dispatch(dispatch)
        except Exception as exc:
            # Пока окно запуска открыто, следующая итерация попробует снова.
            log.error("Could not create dispatch %s: %s", slot.key, exc)

    async def _download_and_prepare(self, source: Source, items: list[CloudItem]) -> list[PreparedImage]:
        payloads = await asyncio.gather(*(self.clouds.download(source, item) for item in items))
        prepared: list[PreparedImage] = []
        for index, (item, payload) in enumerate(zip(items, payloads, strict=True), start=1):
            prepared.append(
                await asyncio.to_thread(
                    prepare_image,
                    item.name,
                    payload,
                    self.settings.max_attachment_bytes,
                    index,
                )
            )
        return prepared

    async def _process_dispatch(self, dispatch: dict) -> None:
        source = await self.database.get_source(int(dispatch["source_id"]))
        if not source:
            await self.database.set_dispatch_status(int(dispatch["id"]), "failed", "Источник удалён")
            return

        dispatch_id = int(dispatch["id"])
        if dispatch["status"] == "pending":
            try:
                prepared = await self._download_and_prepare(source, dispatch["items"])
            except Exception as exc:
                await self.database.set_dispatch_status(dispatch_id, "pending", f"Скачивание: {exc}")
                log.error("Dispatch %s download failed: %s", dispatch_id, exc)
                return

            all_delivered = True
            for guild_id in dispatch["target_guild_ids"]:
                if await self.database.delivery_succeeded(dispatch_id, guild_id):
                    continue
                channel_id = await self.database.get_guild_channel(guild_id)
                if channel_id is None or self.client.get_guild(guild_id) is None:
                    # Сервер отключил рассылку или удалил бота после создания задания.
                    await self.database.record_delivery(
                        dispatch_id, guild_id, channel_id, success=True, error="Рассылка на сервере отключена"
                    )
                    continue
                try:
                    message_id = await self._send(channel_id, prepared)
                    await self.database.record_delivery(
                        dispatch_id, guild_id, channel_id, success=True, message_id=message_id
                    )
                except Exception as exc:
                    all_delivered = False
                    await self.database.record_delivery(
                        dispatch_id, guild_id, channel_id, success=False, error=str(exc)[:1000]
                    )
                    log.warning("Dispatch %s failed for guild %s: %s", dispatch_id, guild_id, exc)

            if not all_delivered:
                await self.database.set_dispatch_status(dispatch_id, "pending", "Доставка будет повторена")
                return
            await self.database.set_dispatch_status(dispatch_id, "deleting")

        try:
            for item in dispatch["items"]:
                await self.clouds.trash(source, item)
        except Exception as exc:
            await self.database.set_dispatch_status(dispatch_id, "deleting", f"Удаление: {exc}")
            log.error("Dispatch %s cloud cleanup failed: %s", dispatch_id, exc)
            return
        await self.database.set_dispatch_status(dispatch_id, "completed")
        log.info("Dispatch %s completed and source files moved to trash", dispatch_id)

    async def _send(self, channel_id: int, images: list[PreparedImage]) -> int:
        channel = self.client.get_channel(channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise RuntimeError("Настроенный канал недоступен или не является текстовым")

        files = [discord.File(io.BytesIO(image.data), filename=image.filename) for image in images]
        names = "\n".join(discord.utils.escape_markdown(image.display_name) for image in images)
        embed = discord.Embed(description=names, colour=discord.Colour(images[0].colour))
        embed.set_image(url=f"attachment://{images[0].filename}")
        if len(images) > 1:
            embed.set_thumbnail(url=f"attachment://{images[1].filename}")
        message = await channel.send(embed=embed, files=files, allowed_mentions=discord.AllowedMentions.none())
        return message.id
