"""Suivi des vidéos envoyées en validation.

v0.2 : stockage en mémoire, suffisant pour tester le circuit. Il sera remplacé
par une table Postgres (même interface) avant la mise en production, pour ne
perdre aucune décision lors d'un redéploiement.
"""
from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"


@dataclass
class Review:
    id: str
    account: str              # "clips" | "stories" | "test"
    video_path: str
    caption: str
    status: str = PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    decided_at: datetime | None = None


class AlreadyDecided(Exception):
    pass


class ReviewStore:
    def __init__(self) -> None:
        self._items: dict[str, Review] = {}
        self._lock = threading.Lock()

    def create(self, account: str, video_path: str, caption: str) -> Review:
        r = Review(id=secrets.token_urlsafe(9), account=account, video_path=video_path, caption=caption)
        with self._lock:
            self._items[r.id] = r
        return r

    def get(self, review_id: str) -> Review | None:
        return self._items.get(review_id)

    def decide(self, review_id: str, approved: bool) -> Review:
        """Une décision est définitive : un second clic est refusé (pas de double publication)."""
        with self._lock:
            r = self._items.get(review_id)
            if r is None:
                raise KeyError(review_id)
            if r.status != PENDING:
                raise AlreadyDecided(r.status)
            r.status = APPROVED if approved else REJECTED
            r.decided_at = datetime.now(timezone.utc)
            return r
