from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials

from .models import Category, CloudItem, Source

log = logging.getLogger(__name__)

GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}


class CloudError(RuntimeError):
    pass


def is_image(name: str, mime_type: str | None) -> bool:
    return bool(
        (mime_type or "").lower().startswith("image/") or PurePosixPath(name).suffix.lower() in IMAGE_EXTENSIONS
    )


def google_folder_id(value: str) -> str:
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        match = re.search(r"/folders/([A-Za-z0-9_-]+)", parsed.path)
        if match:
            return match.group(1)
        query_id = parse_qs(parsed.query).get("id")
        if query_id:
            return query_id[0]
        raise CloudError("Не удалось найти ID папки в ссылке Google Drive")
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", value):
        return value
    raise CloudError("Укажите ссылку на папку Google Drive или её ID")


def normalize_yandex_path(value: str) -> str:
    value = value.strip().replace("\\", "/")
    if not value:
        raise CloudError("Путь к папке Яндекс Диска не может быть пустым")
    if value.startswith(("http://", "https://")):
        raise CloudError("Для удаления нужен приватный путь, например /BotImages, а не публичная ссылка")
    return "/" + value.strip("/")


class CloudProvider(ABC):
    def __init__(self, session: aiohttp.ClientSession, max_download_bytes: int):
        self.session = session
        self.max_download_bytes = max_download_bytes

    @abstractmethod
    async def categories(self, source: Source) -> list[Category]:
        raise NotImplementedError

    @abstractmethod
    async def download(self, source: Source, item: CloudItem) -> bytes:
        raise NotImplementedError

    @abstractmethod
    async def trash(self, source: Source, item: CloudItem) -> None:
        raise NotImplementedError

    async def _download_url(self, url: str, *, headers: dict[str, str] | None = None) -> bytes:
        async with self.session.get(url, headers=headers, allow_redirects=True) as response:
            if response.status >= 400:
                raise CloudError(await _response_error(response))
            declared = response.content_length
            if declared is not None and declared > self.max_download_bytes:
                raise CloudError(f"Файл больше лимита {self.max_download_bytes // 1024 // 1024} МБ")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.content.iter_chunked(256 * 1024):
                size += len(chunk)
                if size > self.max_download_bytes:
                    raise CloudError(f"Файл больше лимита {self.max_download_bytes // 1024 // 1024} МБ")
                chunks.append(chunk)
            return b"".join(chunks)


async def _response_error(response: aiohttp.ClientResponse) -> str:
    try:
        payload = await response.json(content_type=None)
        detail = payload.get("message") or payload.get("error_description") or payload.get("error")
    except Exception:
        detail = (await response.text())[:300]
    return f"Облако ответило {response.status}: {detail or response.reason}"


class GoogleDriveProvider(CloudProvider):
    API = "https://www.googleapis.com/drive/v3"

    def __init__(
        self,
        session: aiohttp.ClientSession,
        max_download_bytes: int,
        credentials_file: str | None,
        oauth_token_file: str | None,
    ):
        super().__init__(session, max_download_bytes)
        try:
            if oauth_token_file and Path(oauth_token_file).is_file():
                self.credentials = UserCredentials.from_authorized_user_file(
                    oauth_token_file, scopes=["https://www.googleapis.com/auth/drive"]
                )
            elif credentials_file:
                self.credentials = service_account.Credentials.from_service_account_file(
                    credentials_file,
                    scopes=["https://www.googleapis.com/auth/drive"],
                )
            else:
                raise CloudError(
                    "Нет доступа Google: выполните python start.py google-login или задайте GOOGLE_SERVICE_ACCOUNT_FILE"
                )
        except Exception as exc:
            if isinstance(exc, CloudError):
                raise
            raise CloudError(f"Не удалось прочитать ключ Google: {exc}") from exc
        self._token_lock = asyncio.Lock()
        self._oauth_token_file = Path(oauth_token_file) if oauth_token_file else None

    async def _headers(self) -> dict[str, str]:
        async with self._token_lock:
            stale = not self.credentials.token or self.credentials.expired
            if stale:
                try:
                    await asyncio.to_thread(self.credentials.refresh, Request())
                    if isinstance(self.credentials, UserCredentials) and self._oauth_token_file:
                        await asyncio.to_thread(
                            self._oauth_token_file.write_text,
                            self.credentials.to_json(),
                            encoding="utf-8",
                        )
                except Exception as exc:
                    raise CloudError(f"Не удалось авторизоваться в Google Drive: {exc}") from exc
        return {"Authorization": f"Bearer {self.credentials.token}"}

    async def _list_children(self, folder_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, str] = {
                "q": f"'{folder_id}' in parents and trashed = false",
                "pageSize": "1000",
                "fields": "nextPageToken,files(id,name,mimeType,size,capabilities(canTrash))",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token
            async with self.session.get(f"{self.API}/files", params=params, headers=await self._headers()) as response:
                if response.status >= 400:
                    raise CloudError(await _response_error(response))
                payload = await response.json()
            result.extend(payload.get("files", []))
            page_token = payload.get("nextPageToken")
            if not page_token:
                return result

    async def _images_recursive(self, folder_id: str) -> tuple[CloudItem, ...]:
        queue = [folder_id]
        images: list[CloudItem] = []
        visited: set[str] = set()
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            for item in await self._list_children(current):
                mime = str(item.get("mimeType") or "")
                if mime == GOOGLE_FOLDER_MIME:
                    queue.append(str(item["id"]))
                elif is_image(str(item.get("name") or ""), mime):
                    if item.get("capabilities", {}).get("canTrash") is not True:
                        continue
                    images.append(
                        CloudItem(
                            id=str(item["id"]),
                            name=str(item.get("name") or item["id"]),
                            mime_type=mime or "application/octet-stream",
                            size=int(item["size"]) if item.get("size") else None,
                        )
                    )
            if len(images) > 10_000:
                raise CloudError("В одной категории больше 10 000 изображений; разделите её на несколько папок")
        return tuple(images)

    async def categories(self, source: Source) -> list[Category]:
        root_id = google_folder_id(source.location)
        categories: list[Category] = []
        for folder in await self._list_children(root_id):
            if folder.get("mimeType") != GOOGLE_FOLDER_MIME:
                continue
            images = await self._images_recursive(str(folder["id"]))
            categories.append(
                Category(source, str(folder["id"]), str(folder.get("name") or "Без названия"), None, images)
            )
        return categories

    async def download(self, source: Source, item: CloudItem) -> bytes:
        url = f"{self.API}/files/{item.id}?alt=media&supportsAllDrives=true"
        return await self._download_url(url, headers=await self._headers())

    async def trash(self, source: Source, item: CloudItem) -> None:
        url = f"{self.API}/files/{item.id}"
        params = {"supportsAllDrives": "true"}
        async with self.session.patch(
            url, params=params, json={"trashed": True}, headers=await self._headers()
        ) as response:
            if response.status == 404:
                return
            if response.status >= 400:
                raise CloudError(await _response_error(response))


class YandexDiskProvider(CloudProvider):
    API = "https://cloud-api.yandex.net/v1/disk"

    def __init__(self, session: aiohttp.ClientSession, max_download_bytes: int, token: str | None):
        super().__init__(session, max_download_bytes)
        if not token:
            raise CloudError("Не задан YANDEX_DISK_TOKEN")
        self.headers = {"Authorization": f"OAuth {token}"}

    async def _list_children(self, path: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        offset = 0
        while True:
            params = {"path": path, "limit": "1000", "offset": str(offset)}
            async with self.session.get(f"{self.API}/resources", params=params, headers=self.headers) as response:
                if response.status >= 400:
                    raise CloudError(await _response_error(response))
                payload = await response.json()
            embedded = payload.get("_embedded", {})
            items = embedded.get("items", [])
            result.extend(items)
            offset += len(items)
            if not items or offset >= int(embedded.get("total", len(result))):
                return result

    async def _images_recursive(self, folder_path: str) -> tuple[CloudItem, ...]:
        queue = [folder_path]
        images: list[CloudItem] = []
        visited: set[str] = set()
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            for item in await self._list_children(current):
                item_type = item.get("type")
                item_path = str(item.get("path") or "")
                if item_type == "dir":
                    queue.append(item_path)
                elif item_type == "file" and is_image(str(item.get("name") or ""), item.get("mime_type")):
                    images.append(
                        CloudItem(
                            id=item_path,
                            path=item_path,
                            name=str(item.get("name") or "image"),
                            mime_type=str(item.get("mime_type") or "application/octet-stream"),
                            size=int(item["size"]) if item.get("size") else None,
                        )
                    )
            if len(images) > 10_000:
                raise CloudError("В одной категории больше 10 000 изображений; разделите её на несколько папок")
        return tuple(images)

    async def categories(self, source: Source) -> list[Category]:
        root = normalize_yandex_path(source.location)
        categories: list[Category] = []
        for folder in await self._list_children(root):
            if folder.get("type") != "dir":
                continue
            path = str(folder.get("path") or "")
            images = await self._images_recursive(path)
            categories.append(Category(source, path, str(folder.get("name") or "Без названия"), path, images))
        return categories

    async def download(self, source: Source, item: CloudItem) -> bytes:
        path = item.path or item.id
        async with self.session.get(
            f"{self.API}/resources/download", params={"path": path}, headers=self.headers
        ) as response:
            if response.status >= 400:
                raise CloudError(await _response_error(response))
            href = (await response.json()).get("href")
        if not href:
            raise CloudError("Яндекс Диск не вернул ссылку для скачивания")
        return await self._download_url(str(href))

    async def trash(self, source: Source, item: CloudItem) -> None:
        path = item.path or item.id
        params = {"path": path, "permanently": "false", "force_async": "false"}
        async with self.session.delete(f"{self.API}/resources", params=params, headers=self.headers) as response:
            if response.status == 404:
                return
            if response.status not in {202, 204}:
                raise CloudError(await _response_error(response))


class CloudManager:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        max_download_bytes: int,
        google_credentials_file: str | None,
        google_oauth_token_file: str | None,
        yandex_token: str | None,
    ):
        self.session = session
        self.max_download_bytes = max_download_bytes
        self.google_credentials_file = google_credentials_file
        self.google_oauth_token_file = google_oauth_token_file
        self.yandex_token = yandex_token
        self._providers: dict[str, CloudProvider] = {}

    def provider(self, kind: str) -> CloudProvider:
        if kind in self._providers:
            return self._providers[kind]
        if kind == "google_drive":
            provider: CloudProvider = GoogleDriveProvider(
                self.session,
                self.max_download_bytes,
                self.google_credentials_file,
                self.google_oauth_token_file,
            )
        elif kind == "yandex_disk":
            provider = YandexDiskProvider(self.session, self.max_download_bytes, self.yandex_token)
        else:
            raise CloudError(f"Неизвестный тип источника: {kind}")
        self._providers[kind] = provider
        return provider

    async def categories(self, source: Source) -> list[Category]:
        return await self.provider(source.kind).categories(source)

    async def download(self, source: Source, item: CloudItem) -> bytes:
        return await self.provider(source.kind).download(source, item)

    async def trash(self, source: Source, item: CloudItem) -> None:
        await self.provider(source.kind).trash(source, item)

    async def discover_eligible(
        self, sources: list[Source], images_needed: int
    ) -> tuple[list[Category], dict[str, str]]:
        eligible: list[Category] = []
        errors: dict[str, str] = {}

        async def scan(source: Source) -> None:
            try:
                categories = await self.categories(source)
                eligible.extend(category for category in categories if len(category.images) >= images_needed)
            except Exception as exc:
                log.warning("Source %s scan failed: %s", source.name, exc)
                errors[source.name] = str(exc)

        await asyncio.gather(*(scan(source) for source in sources))
        return eligible, errors
