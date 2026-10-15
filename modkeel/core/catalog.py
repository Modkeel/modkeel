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


@dataclass
class MissingBuild:
    file: str                      # the JAR in the mods folder
    project_id: str
    slug: str
    title: str


@dataclass
class BuildsCheck:
    """Which of an instance's mods have a build for its own version and loader, from the
    JARs' hashes: two Modrinth requests, however many mods. Only JARs Modrinth knows by hash
    are judged; the rest (CurseForge-only, local builds) are counted as unknown."""
    mods: int                      # JARs in the folder
    known: int                     # of those, files Modrinth knows by hash
    missing: List[MissingBuild]    # known, but their project has no build for mc + loader
    reason: str = ""               # why nothing could be judged (Modrinth down), else ""

    def to_dict(self) -> Dict:
        return {"mods": self.mods, "known": self.known, "reason": self.reason,
                "missing": [dict(m.__dict__) for m in self.missing]}


def _post(url: str, body: Dict) -> Dict:
    try:
        resp = modrinth_call(requests.post, url, json=body,
                             headers={"User-Agent": MODRINTH_USER_AGENT}, timeout=30)
    except requests.RequestException as e:
        raise CatalogError("Modrinth timed out" if isinstance(e, requests.Timeout)
                           else f"Modrinth unreachable: {e.__class__.__name__}") from e
    if resp.status_code != 200:
        raise CatalogError(f"Modrinth answered HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as e:
        raise CatalogError("Modrinth sent an unreadable answer") from e


def builds_check(mods_dir: str, mc_version: str, loader: str) -> BuildsCheck:
    """The mods in `mods_dir` whose Modrinth project has no build for `mc_version` + `loader`
    (the instance runs them anyway: an older build, or one for another loader). Modrinth
    down is a check with a reason, never an error."""
    from pathlib import Path

    from modkeel.scanner import compute_sha1

    loader = loader.lower()
    jars = sorted(Path(mods_dir).glob("*.jar"))
    try:
        hashes = {compute_sha1(j): j.name for j in jars}
    except OSError as e:
        return BuildsCheck(len(jars), 0, [], f"could not read the mods folder: {e}")
    if not hashes:
        return BuildsCheck(0, 0, [])
    base = "https://api.modrinth.com/v2/version_files"
    try:
        known = _post(base, {"hashes": list(hashes), "algorithm": "sha1"})
        # the newest build of each file's own project for this version and loader, if any
        have = _post(f"{base}/update", {"hashes": list(known), "algorithm": "sha1",
                                        "loaders": [loader], "game_versions": [mc_version]})
    except CatalogError as e:
        return BuildsCheck(len(jars), 0, [], str(e))
    lacking = {h: v["project_id"] for h, v in known.items() if h not in have}
    projects: Dict[str, Dict] = {}
    if lacking:
        try:
            resp = modrinth_call(requests.get, "https://api.modrinth.com/v2/projects",
                                 params={"ids": json.dumps(sorted(set(lacking.values())))},
                                 headers={"User-Agent": MODRINTH_USER_AGENT}, timeout=30)
            if resp.status_code == 200:
                projects = {p["id"]: p for p in resp.json()}
        except (requests.RequestException, ValueError, KeyError):
            pass                   # names are a nicety: the file names still say which
    missing = []
    for sha, pid in lacking.items():
        project = projects.get(pid, {})
        missing.append(MissingBuild(hashes[sha], pid, project.get("slug") or "",
                                    project.get("title") or hashes[sha]))
    return BuildsCheck(len(jars), len(known), sorted(missing, key=lambda m: m.title.lower()))
