"""Search the catalog for a front end's "Add a mod" screen: who each hit is, and what is
possible on the player's version.

A name is not an identity: "voxy" matches eight projects (the mod, its addons, a world
generator). The player picks one from the list by its icon, author and downloads, or opens
its page to check, and the app then asks for that project by id (GetRequest.project_id), so
nothing is guessed from the name a second time.

The search itself has no version or loader filter (the same rule as find_project: identify
first, then look for builds), so a mod with no build for the target still shows up, marked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import json

import requests

from modkeel.constants import MODRINTH_USER_AGENT
from modkeel.modrinth import modrinth_call

PAGE_URL = "https://modrinth.com/mod/{slug}"

# What Modkeel can say about a hit on the player's version and loader, before running anything.
OFFICIAL = "official"              # the author publishes a build for this version and loader
OTHER_VERSIONS = "other_versions"  # builds for this loader, not this version: a port or an
                                   # older build may work (get tries them)
OTHER_LOADER = "other_loader"      # no build for this loader at all
STATUSES = (OFFICIAL, OTHER_VERSIONS, OTHER_LOADER)


class CatalogError(Exception):
    """Modrinth could not be asked (network, HTTP error); the message says why."""


@dataclass
class SearchHit:
    project_id: str
    slug: str
    title: str
    author: str
    downloads: int
    icon_url: Optional[str]
    description: str
    page_url: str
    status: str                    # one of STATUSES

    def to_dict(self) -> Dict:
        return dict(self.__dict__)


def _status(hit: Dict, mc_version: str, loader: str,
            builds: Callable[[str], Optional[List[Dict]]]) -> str:
    """A search hit lists every game version and loader the project has, but not which go
    together: 1.21.11 may be Fabric only and NeoForge stop at 1.21.1. When both appear, one
    versions call settles it."""
    if loader not in hit.get("categories", []):
        return OTHER_LOADER
    if mc_version not in hit.get("versions", []):
        return OTHER_VERSIONS
    found = builds(hit["project_id"])
    # Modrinth could not answer: the hit's own lists said yes, keep that
    return OFFICIAL if found is None or found else OTHER_VERSIONS


def _builds(project_id: str, loader: str, mc_version: str) -> Optional[List[Dict]]:
    """The project's builds for loader + version; None when Modrinth could not answer."""
    try:
        resp = modrinth_call(
            requests.get, f"https://api.modrinth.com/v2/project/{project_id}/version",
            params={"loaders": json.dumps([loader]), "game_versions": json.dumps([mc_version])},
            headers={"User-Agent": MODRINTH_USER_AGENT}, timeout=15)
        return resp.json() if resp.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def search_mods(query: str, mc_version: str, loader: str, limit: int = 10) -> List[SearchHit]:
    """Modrinth's mods for `query`, in its relevance order, each with its status on
    `mc_version` + `loader`. Raises CatalogError when Modrinth cannot be asked."""
    loader = loader.lower()
    try:
        resp = modrinth_call(
            requests.get, "https://api.modrinth.com/v2/search",
            params={"query": query, "facets": '[["project_type:mod"]]', "limit": limit},
            headers={"User-Agent": MODRINTH_USER_AGENT}, timeout=15)
    except requests.RequestException as e:
        raise CatalogError("Modrinth timed out" if isinstance(e, requests.Timeout)
                           else f"Modrinth unreachable: {e.__class__.__name__}") from e
    if resp.status_code != 200:
        raise CatalogError(f"Modrinth answered HTTP {resp.status_code}")
    try:
        hits = resp.json().get("hits", [])
    except ValueError as e:
        raise CatalogError("Modrinth sent an unreadable answer") from e

    def builds(project_id: str) -> Optional[List[Dict]]:
        return _builds(project_id, loader, mc_version)

    return [SearchHit(
        project_id=h["project_id"], slug=h["slug"], title=h.get("title") or h["slug"],
        author=h.get("author") or "", downloads=int(h.get("downloads") or 0),
        icon_url=h.get("icon_url") or None, description=h.get("description") or "",
        page_url=PAGE_URL.format(slug=h["slug"]),
        status=_status(h, mc_version, loader, builds),
    ) for h in hits if h.get("project_id") and h.get("slug")]
