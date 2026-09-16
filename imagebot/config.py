from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} должен быть целым числом") from exc
    if value <= 0:
        raise ValueError(f"{name} должен быть больше нуля")
    return value


def parse_schedule_times(raw: str, *, expected_count: int | None = None) -> tuple[str, ...]:
    result: list[str] = []
    for item in raw.split(","):
        item = item.strip()
        try:
            hour_text, minute_text = item.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
        except (ValueError, AttributeError) as exc:
            raise ValueError("SCHEDULE_TIMES задаётся в виде 07:00,14:00,19:00") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"Некорректное время в SCHEDULE_TIMES: {item}")
        normalized = f"{hour:02d}:{minute:02d}"
        if normalized in result:
            raise ValueError(f"Время {normalized} указано несколько раз")
        result.append(normalized)
    if not result:
        raise ValueError("SCHEDULE_TIMES не может быть пустым")
    if expected_count is not None and len(result) != expected_count:
        raise ValueError(f"Нужно указать ровно {expected_count} разных времени")
    return tuple(sorted(result))


@dataclass(frozen=True, slots=True)
class Settings:
    discord_token: str
    bootstrap_secret: str
    timezone_name: str
    schedule_times: tuple[str, ...]
    images_per_post: int
    catchup_window_minutes: int
    max_attachment_bytes: int
    max_download_bytes: int
    database_path: Path
    google_service_account_file: str | None
    google_oauth_client_file: str | None
    google_oauth_token_file: Path
    yandex_disk_token: str | None
    log_level: str
    dev_guild_id: int | None

    @property
    def timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Неизвестный часовой пояс: {self.timezone_name}") from exc

    @classmethod
    def load(cls, *, require_discord_token: bool = True) -> Settings:
        load_dotenv()
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if require_discord_token and not token:
            raise ValueError("Не задан DISCORD_TOKEN. Скопируйте .env.example в .env и заполните его.")

        dev_guild_raw = os.getenv("DEV_GUILD_ID", "").strip()
        try:
            dev_guild_id = int(dev_guild_raw) if dev_guild_raw else None
        except ValueError as exc:
            raise ValueError("DEV_GUILD_ID должен быть числовым ID сервера") from exc

        database_path = Path(os.getenv("DATABASE_PATH", "data/bot.db").strip())
        settings = cls(
            discord_token=token,
            bootstrap_secret=os.getenv("BOOTSTRAP_SECRET", "").strip(),
            timezone_name=os.getenv("TIMEZONE", "Europe/Moscow").strip(),
            schedule_times=parse_schedule_times(os.getenv("SCHEDULE_TIMES", "07:00,14:00,19:00")),
            images_per_post=_positive_int("IMAGES_PER_POST", 2),
            catchup_window_minutes=_positive_int("CATCHUP_WINDOW_MINUTES", 15),
            max_attachment_bytes=_positive_int("MAX_ATTACHMENT_MB", 8) * 1024 * 1024,
            max_download_bytes=_positive_int("MAX_DOWNLOAD_MB", 50) * 1024 * 1024,
            database_path=database_path,
            google_service_account_file=os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip() or None,
            google_oauth_client_file=os.getenv("GOOGLE_OAUTH_CLIENT_FILE", "").strip() or None,
            google_oauth_token_file=Path(os.getenv("GOOGLE_OAUTH_TOKEN_FILE", "data/google-token.json").strip()),
            yandex_disk_token=os.getenv("YANDEX_DISK_TOKEN", "").strip() or None,
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            dev_guild_id=dev_guild_id,
        )
        # Падаем при запуске, а не молча пропускаем рассылку из-за опечатки.
        _ = settings.timezone
        return settings
