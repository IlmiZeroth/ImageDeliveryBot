from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from imagebot.bot import create_bot
from imagebot.config import Settings
from imagebot.database import Database


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Discord-бот рассылки изображений")
    commands = root.add_subparsers(dest="command")

    commands.add_parser("google-login", help="Один раз подключить личный Google Drive")

    owner = commands.add_parser("superadmin", help="Управление суперадминами без запуска Discord")
    owner_commands = owner.add_subparsers(dest="owner_command", required=True)
    owner_commands.add_parser("list", help="Показать список")
    add = owner_commands.add_parser("add", help="Добавить Discord user ID")
    add.add_argument("user_id", type=int)
    remove = owner_commands.add_parser("remove", help="Удалить Discord user ID")
    remove.add_argument("user_id", type=int)
    clear = owner_commands.add_parser("clear", help="Удалить всех суперадминов")
    clear.add_argument("--yes", action="store_true", help="Подтвердить очистку")
    return root


def google_login(settings: Settings) -> int:
    if not settings.google_oauth_client_file:
        print("Ошибка: задайте GOOGLE_OAUTH_CLIENT_FILE в .env.", file=sys.stderr)
        return 2
    client_file = Path(settings.google_oauth_client_file)
    if not client_file.is_file():
        print(f"Ошибка: файл OAuth-клиента не найден: {client_file}", file=sys.stderr)
        return 2
    flow = InstalledAppFlow.from_client_secrets_file(str(client_file), ["https://www.googleapis.com/auth/drive"])
    credentials = flow.run_local_server(port=0, open_browser=True, prompt="consent")
    settings.google_oauth_token_file.parent.mkdir(parents=True, exist_ok=True)
    settings.google_oauth_token_file.write_text(credentials.to_json(), encoding="utf-8")
    print(f"Google Drive подключён. Токен сохранён: {settings.google_oauth_token_file}")
    return 0


async def manage_superadmins(settings: Settings, args: argparse.Namespace) -> int:
    database = Database(settings.database_path)
    await database.initialize()
    if args.owner_command == "list":
        owners = await database.list_superadmins()
        print("\n".join(str(user_id) for user_id in owners) if owners else "Список суперадминов пуст.")
        return 0
    if args.owner_command == "add":
        created = await database.add_superadmin(args.user_id, None)
        print("Суперадмин добавлен." if created else "Этот пользователь уже суперадмин.")
        return 0
    if args.owner_command == "remove":
        removed = await database.remove_superadmin(args.user_id)
        print("Суперадмин удалён." if removed else "Пользователь не найден.")
        return 0
    if args.owner_command == "clear":
        if not args.yes:
            print("Очистка отменена: добавьте --yes для подтверждения.", file=sys.stderr)
            return 2
        count = await database.clear_superadmins()
        print(f"Удалено суперадминов: {count}.")
        return 0
    return 2


async def run_bot(settings: Settings) -> None:
    database = Database(settings.database_path)
    await database.initialize()
    bot = create_bot(settings, database)
    async with bot:
        await bot.start(settings.discord_token, reconnect=True)


def main() -> int:
    args = parser().parse_args()
    try:
        settings = Settings.load(require_discord_token=args.command not in {"superadmin", "google-login"})
        logging.basicConfig(
            level=getattr(logging, settings.log_level, logging.INFO),
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        )
        if args.command == "superadmin":
            return asyncio.run(manage_superadmins(settings, args))
        if args.command == "google-login":
            return google_login(settings)
        asyncio.run(run_bot(settings))
        return 0
    except (ValueError, KeyboardInterrupt) as exc:
        if str(exc):
            print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
