from __future__ import annotations

import hmac
import io
import logging
import time
from collections import defaultdict

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from .cloud import CloudError, CloudManager, google_folder_id, normalize_yandex_path
from .config import Settings, parse_schedule_times
from .database import Database
from .models import Source
from .scheduler import BroadcastService

log = logging.getLogger(__name__)
BRAND = 0x5865F2
SUCCESS = 0x57F287
WARNING = 0xFEE75C
ERROR = 0xED4245


def secrets_match(provided: str, expected: str) -> bool:
    """Constant-time comparison that also supports non-ASCII secrets."""
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def card(title: str, description: str, colour: int = BRAND) -> discord.Embed:
    return discord.Embed(title=title, description=description, colour=discord.Colour(colour))


class ImageBot(commands.Bot):
    def __init__(self, settings: Settings, database: Database):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.settings = settings
        self.database = database
        self.http_session: aiohttp.ClientSession | None = None
        self.clouds: CloudManager | None = None
        self.broadcasts: BroadcastService | None = None

    async def setup_hook(self) -> None:
        timeout = aiohttp.ClientTimeout(total=120, connect=20)
        self.http_session = aiohttp.ClientSession(timeout=timeout)
        self.clouds = CloudManager(
            self.http_session,
            max_download_bytes=self.settings.max_download_bytes,
            google_credentials_file=self.settings.google_service_account_file,
            google_oauth_token_file=str(self.settings.google_oauth_token_file),
            yandex_token=self.settings.yandex_disk_token,
        )
        self.broadcasts = BroadcastService(self, self.database, self.clouds, self.settings)
        register_commands(self)
        if self.settings.dev_guild_id:
            guild = discord.Object(id=self.settings.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Commands synced to development guild %s", guild.id)
        else:
            await self.tree.sync()
            log.info("Global commands synced")
        self.broadcasts.start()

    async def close(self) -> None:
        if self.broadcasts:
            self.broadcasts.stop()
        if self.http_session:
            await self.http_session.close()
        await super().close()

    async def on_ready(self) -> None:
        if not self.user:
            return
        permissions = discord.Permissions(view_channel=True, send_messages=True, embed_links=True, attach_files=True)
        invite = discord.utils.oauth_url(self.user.id, permissions=permissions, scopes=("bot", "applications.commands"))
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        log.info("Invite URL: %s", invite)


async def _send_ephemeral(interaction: discord.Interaction, embed: discord.Embed) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def _require_superadmin(bot: ImageBot, interaction: discord.Interaction) -> bool:
    if await bot.database.is_superadmin(interaction.user.id):
        return True
    await _send_ephemeral(
        interaction,
        card("Доступ закрыт", "Эта команда доступна только суперадминам бота.", ERROR),
    )
    return False


async def _require_server_admin(interaction: discord.Interaction) -> bool:
    if (
        interaction.guild
        and isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    ):
        return True
    await _send_ephemeral(
        interaction,
        card("Нужны права администратора", "Настроить канал может администратор этого сервера.", ERROR),
    )
    return False


def register_commands(bot: ImageBot) -> None:
    activation_attempts: dict[int, list[float]] = defaultdict(list)

    channel_group = app_commands.Group(name="канал", description="Канал рассылки на этом сервере")
    source_group = app_commands.Group(name="источник", description="Облачные источники изображений")
    owner_group = app_commands.Group(name="суперадмин", description="Управление суперадминами бота")
    mailing_group = app_commands.Group(name="рассылка", description="Состояние и проверка рассылки")

    @channel_group.command(name="установить", description="Назначить канал для изображений")
    @app_commands.describe(канал="Канал рассылки; если не указан, используется текущий")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def set_channel(interaction: discord.Interaction, канал: discord.TextChannel | None = None) -> None:
        if not await _require_server_admin(interaction) or not interaction.guild:
            return
        target = канал or (interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None)
        if target is None:
            await _send_ephemeral(interaction, card("Выберите канал", "Укажите обычный текстовый канал.", ERROR))
            return
        me = interaction.guild.me
        if me is None:
            await _send_ephemeral(interaction, card("Не удалось проверить права", "Попробуйте ещё раз.", ERROR))
            return
        permissions = target.permissions_for(me)
        missing = []
        for allowed, label in (
            (permissions.view_channel, "просмотр канала"),
            (permissions.send_messages, "отправка сообщений"),
            (permissions.embed_links, "встраивание ссылок"),
            (permissions.attach_files, "прикрепление файлов"),
        ):
            if not allowed:
                missing.append(label)
        if missing:
            await _send_ephemeral(
                interaction,
                card("Боту не хватает прав", f"В {target.mention} разрешите: **{', '.join(missing)}**.", ERROR),
            )
            return
        await bot.database.set_guild_channel(interaction.guild.id, target.id, interaction.user.id)
        await _send_ephemeral(
            interaction,
            card(
                "Канал подключён",
                f"Изображения будут приходить в {target.mention}. Больше на сервере настраивать ничего не нужно.",
                SUCCESS,
            ),
        )

    @channel_group.command(name="статус", description="Показать текущий канал рассылки")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def channel_status(interaction: discord.Interaction) -> None:
        if not await _require_server_admin(interaction) or not interaction.guild:
            return
        channel_id = await bot.database.get_guild_channel(interaction.guild.id)
        description = (
            f"Рассылка включена: <#{channel_id}>."
            if channel_id
            else "Канал ещё не назначен. Используйте `/канал установить`."
        )
        await _send_ephemeral(interaction, card("Канал рассылки", description, SUCCESS if channel_id else WARNING))

    @channel_group.command(name="отключить", description="Отключить рассылку на этом сервере")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def disable_channel(interaction: discord.Interaction) -> None:
        if not await _require_server_admin(interaction) or not interaction.guild:
            return
        changed = await bot.database.disable_guild(interaction.guild.id)
        text = "Рассылка на этом сервере отключена." if changed else "Рассылка уже была отключена."
        await _send_ephemeral(interaction, card("Готово", text, SUCCESS))

    @owner_group.command(name="активировать", description="Активировать права суперадмина по секретному коду")
    @app_commands.describe(код="Секрет из BOOTSTRAP_SECRET")
    async def activate_owner(interaction: discord.Interaction, код: str) -> None:
        now = time.monotonic()
        attempts = [stamp for stamp in activation_attempts[interaction.user.id] if now - stamp < 600]
        activation_attempts[interaction.user.id] = attempts
        if len(attempts) >= 5:
            await _send_ephemeral(interaction, card("Слишком много попыток", "Повторите через 10 минут.", ERROR))
            return
        if not bot.settings.bootstrap_secret:
            await _send_ephemeral(
                interaction,
                card("Активация отключена", "Добавьте первого суперадмина через консоль.", WARNING),
            )
            return
        if not secrets_match(код, bot.settings.bootstrap_secret):
            attempts.append(now)
            await _send_ephemeral(interaction, card("Неверный код", "Проверьте секрет и попробуйте снова.", ERROR))
            return
        created = await bot.database.add_superadmin(interaction.user.id, interaction.user.id)
        text = "Теперь у вас есть права суперадмина." if created else "Вы уже суперадмин."
        await _send_ephemeral(interaction, card("Доступ активирован", text, SUCCESS))

    @owner_group.command(name="добавить", description="Добавить ещё одного суперадмина")
    @app_commands.describe(пользователь="Новый суперадмин")
    async def add_owner(interaction: discord.Interaction, пользователь: discord.User) -> None:
        if not await _require_superadmin(bot, interaction):
            return
        created = await bot.database.add_superadmin(пользователь.id, interaction.user.id)
        text = f"{пользователь.mention} добавлен." if created else f"{пользователь.mention} уже суперадмин."
        await _send_ephemeral(interaction, card("Список обновлён", text, SUCCESS))

    @owner_group.command(name="удалить", description="Снять права с другого суперадмина")
    @app_commands.describe(пользователь="Суперадмин, которого нужно удалить")
    async def remove_owner(interaction: discord.Interaction, пользователь: discord.User) -> None:
        if not await _require_superadmin(bot, interaction):
            return
        if пользователь.id == interaction.user.id:
            await _send_ephemeral(
                interaction,
                card(
                    "Нужен другой суперадмин",
                    "Из Discord нельзя удалить самого себя. Попросите другого суперадмина или используйте консоль.",
                    WARNING,
                ),
            )
            return
        removed = await bot.database.remove_superadmin(пользователь.id)
        text = f"Права {пользователь.mention} сняты." if removed else "Этот пользователь не был суперадмином."
        await _send_ephemeral(interaction, card("Список обновлён", text, SUCCESS))

    @owner_group.command(name="список", description="Показать всех суперадминов")
    async def list_owners(interaction: discord.Interaction) -> None:
        if not await _require_superadmin(bot, interaction):
            return
        owners = await bot.database.list_superadmins()
        text = "\n".join(f"• <@{user_id}>  (`{user_id}`)" for user_id in owners) or "Список пуст."
        await _send_ephemeral(interaction, card("Суперадмины", text))

    source_choices = [
        app_commands.Choice(name="Google Drive", value="google_drive"),
        app_commands.Choice(name="Яндекс Диск", value="yandex_disk"),
    ]

    @source_group.command(name="добавить", description="Подключить корневую папку с категориями")
    @app_commands.describe(
        тип="Облачное хранилище",
        название="Короткое понятное имя источника",
        папка="Google: ссылка/ID папки; Яндекс: приватный путь вида /BotImages",
    )
    @app_commands.choices(тип=source_choices)
    async def add_source(
        interaction: discord.Interaction,
        тип: app_commands.Choice[str],
        название: str,
        папка: str,
    ) -> None:
        if not await _require_superadmin(bot, interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            location = google_folder_id(папка) if тип.value == "google_drive" else normalize_yandex_path(папка)
            candidate = Source(0, название.strip(), тип.value, location)
            if not candidate.name or len(candidate.name) > 60:
                raise ValueError("Название должно содержать от 1 до 60 символов")
            assert bot.clouds is not None
            categories = await bot.clouds.categories(candidate)
            eligible = [item for item in categories if len(item.images) >= bot.settings.images_per_post]
            if not categories:
                raise CloudError("В корневой папке нет папок-категорий")
            source_id = await bot.database.add_source(
                candidate.name, candidate.kind, candidate.location, interaction.user.id
            )
            details = (
                f"Источник **{candidate.name}** подключён под номером `{source_id}`.\n"
                f"Категорий: **{len(categories)}**, готово к рассылке: **{len(eligible)}**."
            )
            if not eligible:
                details += (
                    f"\n\nДобавьте минимум {bot.settings.images_per_post} изображения хотя бы в одну категорию "
                    "и проверьте, что аккаунт бота вправе перемещать их в корзину."
                )
            await interaction.followup.send(embed=card("Источник подключён", details, SUCCESS), ephemeral=True)
        except (CloudError, ValueError, aiosqlite.IntegrityError) as exc:
            message = (
                "Источник с таким названием уже существует." if isinstance(exc, aiosqlite.IntegrityError) else str(exc)
            )
            await interaction.followup.send(embed=card("Не удалось подключить", message, ERROR), ephemeral=True)

    @source_group.command(name="список", description="Показать подключённые источники")
    async def list_sources(interaction: discord.Interaction) -> None:
        if not await _require_superadmin(bot, interaction):
            return
        sources = await bot.database.list_sources(enabled_only=True)
        labels = {"google_drive": "Google Drive", "yandex_disk": "Яндекс Диск"}
        text = (
            "\n".join(
                f"`{source.id}` • **{source.name}** — {labels.get(source.kind, source.kind)}\n  `{source.location}`"
                for source in sources
            )
            or "Активных источников пока нет."
        )
        await _send_ephemeral(interaction, card("Источники", text))

    @source_group.command(name="проверить", description="Проверить папки и количество изображений")
    @app_commands.describe(номер="Номер из команды /источник список")
    async def check_source(interaction: discord.Interaction, номер: int) -> None:
        if not await _require_superadmin(bot, interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        source = await bot.database.get_source(номер)
        if not source or not source.enabled:
            await interaction.followup.send(
                embed=card("Источник не найден", "Проверьте его номер.", ERROR), ephemeral=True
            )
            return
        try:
            assert bot.clouds is not None
            categories = await bot.clouds.categories(source)
            lines = [f"• **{category.name}** — {len(category.images)} изобр." for category in categories[:25]]
            if len(categories) > 25:
                lines.append(f"…и ещё {len(categories) - 25}")
            await interaction.followup.send(
                embed=card("Источник доступен", "\n".join(lines) or "Папок-категорий нет.", SUCCESS), ephemeral=True
            )
        except Exception as exc:
            await interaction.followup.send(embed=card("Проверка не пройдена", str(exc), ERROR), ephemeral=True)

    @source_group.command(name="отключить", description="Отключить источник, сохранив историю")
    @app_commands.describe(номер="Номер из команды /источник список")
    async def remove_source(interaction: discord.Interaction, номер: int) -> None:
        if not await _require_superadmin(bot, interaction):
            return
        removed = await bot.database.remove_source(номер)
        text = "Источник отключён." if removed else "Активный источник с таким номером не найден."
        await _send_ephemeral(interaction, card("Готово", text, SUCCESS if removed else WARNING))

    @mailing_group.command(name="статус", description="Показать общие настройки рассылки")
    async def mailing_status(interaction: discord.Interaction) -> None:
        if not await _require_superadmin(bot, interaction):
            return
        summary = await bot.database.summary()
        schedule_times, _ = await bot.database.get_schedule_times(bot.settings.schedule_times)
        times = " • ".join(schedule_times)
        text = (
            f"**Время:** {times} ({bot.settings.timezone_name})\n"
            f"**Изображений за публикацию:** {bot.settings.images_per_post}\n"
            f"**Серверов:** {summary['guilds']}\n"
            f"**Источников:** {summary['sources']}\n"
            f"**Ожидают завершения:** {summary['pending']}\n"
            f"**Завершено:** {summary['completed']}"
        )
        await _send_ephemeral(interaction, card("Рассылка работает", text, SUCCESS))

    @mailing_group.command(name="расписание", description="Изменить три ежедневных времени рассылки")
    @app_commands.describe(
        время_1="Первое время, например 07:00",
        время_2="Второе время, например 14:00",
        время_3="Третье время, например 19:00",
    )
    async def change_schedule(
        interaction: discord.Interaction,
        время_1: str,
        время_2: str,
        время_3: str,
    ) -> None:
        if not await _require_superadmin(bot, interaction):
            return
        try:
            values = parse_schedule_times(f"{время_1},{время_2},{время_3}", expected_count=3)
        except ValueError as exc:
            await _send_ephemeral(
                interaction,
                card("Проверьте время", f"{exc}\nИспользуйте формат `HH:MM`, например `07:00`.", ERROR),
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        assert bot.broadcasts is not None
        await bot.broadcasts.update_schedule(values, interaction.user.id)
        times = " • ".join(values)
        await interaction.followup.send(
            embed=card(
                "Расписание обновлено",
                f"Новое время: **{times}** ({bot.settings.timezone_name}).\n"
                "Настройка уже действует и сохранена в базе. Перезапуск не нужен.\n\n"
                "Если одно из времён сегодня уже прошло, оно начнёт действовать со следующего дня.",
                SUCCESS,
            ),
            ephemeral=True,
        )

    @mailing_group.command(name="тест", description="Показать случайную пару без публикации и удаления")
    async def mailing_test(interaction: discord.Interaction) -> None:
        if not await _require_superadmin(bot, interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            assert bot.broadcasts is not None
            _, images, _ = await bot.broadcasts.preview()
            files = [discord.File(io.BytesIO(image.data), filename=image.filename) for image in images]
            names = "\n".join(discord.utils.escape_markdown(image.display_name) for image in images)
            embed = discord.Embed(description=names, colour=discord.Colour(images[0].colour))
            embed.set_image(url=f"attachment://{images[0].filename}")
            if len(images) > 1:
                embed.set_thumbnail(url=f"attachment://{images[1].filename}")
            await interaction.followup.send(
                embed=embed,
                files=files,
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(embed=card("Тест не выполнен", str(exc), ERROR), ephemeral=True)

    @bot.tree.command(name="помощь", description="Краткая инструкция по настройке бота")
    async def help_command(interaction: discord.Interaction) -> None:
        is_owner = await bot.database.is_superadmin(interaction.user.id)
        lines = [
            "**Администратору сервера**",
            "`/канал установить` — выбрать единственный канал рассылки.",
            "`/канал статус` — проверить настройку.",
        ]
        if is_owner:
            lines += [
                "",
                "**Суперадмину**",
                "`/источник добавить` — подключить корневую папку.",
                "`/источник проверить` — увидеть категории и остаток файлов.",
                "`/рассылка тест` — безопасный предпросмотр без удаления.",
                "`/рассылка расписание` — изменить три времени без перезапуска.",
                "`/рассылка статус` — общая сводка.",
            ]
        else:
            lines += ["", "Суперадмин активирует доступ командой `/суперадмин активировать` с секретным кодом."]
        await _send_ephemeral(interaction, card("Как пользоваться ботом", "\n".join(lines)))

    for group in (channel_group, source_group, owner_group, mailing_group):
        bot.tree.add_command(group)

    @bot.tree.error
    async def tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        log.exception("Application command failed", exc_info=error)
        await _send_ephemeral(
            interaction, card("Что-то пошло не так", "Ошибка записана в журнал. Попробуйте ещё раз.", ERROR)
        )


def create_bot(settings: Settings, database: Database) -> ImageBot:
    return ImageBot(settings, database)
