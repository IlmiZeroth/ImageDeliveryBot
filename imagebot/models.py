from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Source:
    id: int
    name: str
    kind: str
    location: str
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class CloudItem:
    id: str
    name: str
    mime_type: str
    path: str | None = None
    size: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CloudItem:
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            mime_type=str(data["mime_type"]),
            path=data.get("path"),
            size=int(data["size"]) if data.get("size") is not None else None,
        )


@dataclass(frozen=True, slots=True)
class Category:
    source: Source
    id: str
    name: str
    path: str | None
    images: tuple[CloudItem, ...]
