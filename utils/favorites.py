from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from utils.models import Track


class FavoriteStore:
    def __init__(self, root: str | Path = "data/favorites") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def add(self, user_id: int, track: Track) -> bool:
        path = self.root / f"{user_id}.json"
        items = self._load(path)
        if any(item.get("url") == track.webpage_url for item in items):
            return False

        items.insert(
            0,
            {
                "title": track.title,
                "url": track.webpage_url,
                "uploader": track.uploader,
                "source": track.source,
                "duration": track.duration,
                "thumbnail": track.thumbnail,
            },
        )
        path.write_text(json.dumps(items[:200], ensure_ascii=False, indent=2), encoding="utf-8")
        return True

    def _load(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
