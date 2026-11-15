"""Liste blanche des sources autorisées.

Règle du projet : on ne télécharge JAMAIS une vidéo dont la chaîne ou le domaine
n'est pas dans config/sources_whitelist.yaml (campagnes Whop/Vyro auxquelles on
est inscrit). Toute ingestion passe par `assert_allowed`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

DEFAULT_PATH = Path(os.environ.get(
    "WHITELIST_PATH",
    Path(__file__).resolve().parents[3] / "config" / "sources_whitelist.yaml",
))


class SourceNotAllowed(PermissionError):
    pass


@dataclass(frozen=True)
class Campaign:
    id: str
    platform: str
    rate_per_1k_usd: float
    channels: tuple[str, ...]          # URLs de chaînes / dossiers autorisés (préfixes)
    active: bool = True


def _norm(url: str) -> str:
    u = urlparse(url.strip())
    host = (u.netloc or "").lower().removeprefix("www.").removeprefix("m.")
    return f"{host}{u.path.rstrip('/')}".lower()


def load(path: Path | str = DEFAULT_PATH) -> list[Campaign]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out = []
    for c in data.get("campaigns", []) or []:
        out.append(Campaign(
            id=str(c["id"]),
            platform=str(c.get("platform", "whop")),
            rate_per_1k_usd=float(c.get("rate_per_1k_usd", 0)),
            channels=tuple(str(x) for x in c.get("channels", []) or []),
            active=bool(c.get("active", True)),
        ))
    ids = [c.id for c in out]
    if len(ids) != len(set(ids)):
        raise ValueError("identifiants de campagne en double dans la liste blanche")
    return out


def find_campaign(url: str, campaigns: list[Campaign]) -> Campaign | None:
    target = _norm(url)
    for c in campaigns:
        if not c.active:
            continue
        for ch in c.channels:
            prefix = _norm(ch)
            # préfixe exact sur une frontière de segment : "youtube.com/@abc" n'autorise pas "youtube.com/@abcd"
            if target == prefix or target.startswith(prefix + "/"):
                return c
    return None


def assert_allowed(url: str, campaigns: list[Campaign] | None = None) -> Campaign:
    campaigns = load() if campaigns is None else campaigns
    c = find_campaign(url, campaigns)
    if c is None:
        raise SourceNotAllowed(f"source hors liste blanche : {url}")
    return c
