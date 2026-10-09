"""CurseForge through Modkeel's service: who a JAR is, and a mod's files for a version.

CurseForge's API needs a key issued to Modkeel, and its terms forbid shipping that key inside
a program people download, so this module never calls api.curseforge.com. It asks the routes
of api.modkeel.com that hold the key (/v1/cf/fingerprints, /v1/cf/files) and get back only
what Modkeel needs. When the service is off (no key yet, offline, a self-built copy pointed
nowhere), the client says so once, turns itself off for the rest of the run, and every caller
goes on with Modrinth alone: CurseForge only ever adds results, it never blocks one.

    fingerprint(path)                 CurseForge's file fingerprint (murmur2, see below)
    CurseForgeClient().match(paths)   {path: CfMatch} for the JARs CurseForge knows exactly
    CurseForgeClient().files(ref, mc, loader)
                                      the project (id or slug) and its files for that target

A file whose author allows downloads only from CurseForge itself comes back with url None:
callers point the player to the project page instead of downloading it.

MODKEEL_CURSEFORGE_URL overrides the service's address; empty turns lookups off.
"""

from __future__ import annotations

import logging
import os
import struct
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import requests

from modkeel.constants import MODRINTH_USER_AGENT

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://api.modkeel.com/v1/cf"
# The service takes at most this many fingerprints per request.
MAX_FINGERPRINTS = 1000
TIMEOUT = 20

# Bytes CurseForge drops before hashing a file (tab, newline, carriage return, space), so a
# text file's fingerprint survives line-ending changes; JARs are hashed the same way.
_WHITESPACE = bytes([9, 10, 13, 32])
_M = 0x5BD1E995
_MASK = 0xFFFFFFFF


def murmur2(data: bytes, seed: int = 1) -> int:
    """32-bit MurmurHash2 (Austin Appleby's reference), the hash behind CurseForge's
    fingerprints. Words are read four bytes at a time, little-endian."""
    length = len(data)
    h = (seed ^ length) & _MASK
    whole = length // 4
    for k in struct.unpack_from(f"<{whole}I", data):
        k = (k * _M) & _MASK
        k ^= k >> 24
        h = ((h * _M) & _MASK) ^ ((k * _M) & _MASK)
    tail = data[whole * 4:]
    if len(tail) == 3:
        h ^= tail[2] << 16
    if len(tail) >= 2:
        h ^= tail[1] << 8
    if tail:
        h = ((h ^ tail[0]) * _M) & _MASK
    h ^= h >> 13
    h = (h * _M) & _MASK
    return h ^ (h >> 15)


def fingerprint(path: Path) -> int:
    """CurseForge's fingerprint of a file: murmur2 with seed 1 over its bytes without
    whitespace bytes. Exact, like Modrinth's SHA-1: one fingerprint names one upload."""
    return murmur2(Path(path).read_bytes().translate(None, _WHITESPACE), 1)


@dataclass(frozen=True)
class CfMod:
    """A CurseForge project, as the service describes it."""

    id: int
    slug: str
    name: str
    url: Optional[str] = None          # project page: where a locked file is downloaded
    source: Optional[str] = None       # source repository link, when the author gives one
    distribution: bool = True          # False: files only from CurseForge itself

    @property
    def github_repo(self) -> Optional[str]:
        """"owner/repo" when the source link is a GitHub repository (what forks search)."""
        prefix = "https://github.com/"
        if not self.source or not self.source.lower().startswith(prefix):
            return None
        parts = self.source[len(prefix):].strip("/").split("/")
        return f"{parts[0]}/{parts[1].removesuffix('.git')}" if len(parts) >= 2 else None

    @classmethod
    def of(cls, data: Dict) -> "CfMod":
        return cls(int(data["id"]), data.get("slug") or "", data.get("name") or "",
                   data.get("url"), data.get("source"), data.get("distribution") is not False)


@dataclass(frozen=True)
class CfFile:
    """One upload of a project."""

    id: int
    mod: int
    name: str                          # file name, as it lands in mods/
    display: str = ""
    mc: tuple = ()
    loaders: tuple = ()
    type: str = "release"              # release | beta | alpha
    date: str = ""
    url: Optional[str] = None          # None: the author allows downloads only on CurseForge
    sha1: Optional[str] = None
    fingerprint: Optional[int] = None
    requires: tuple = ()               # project ids of required dependencies

    @classmethod
    def of(cls, data: Dict) -> "CfFile":
        return cls(int(data["id"]), int(data.get("mod") or 0), data.get("name") or "",
                   data.get("display") or data.get("name") or "", tuple(data.get("mc") or ()),
                   tuple(data.get("loaders") or ()), data.get("type") or "release",
                   data.get("date") or "", data.get("url"), data.get("sha1"),
                   data.get("fingerprint"), tuple(data.get("requires") or ()))


@dataclass(frozen=True)
class CfMatch:
    """A JAR CurseForge knows by its fingerprint: the exact upload and its project."""

    mod: CfMod
    file: CfFile


@dataclass
class CfProject:
    """A project and its files for one Minecraft version and loader, newest first."""

    mod: Optional[CfMod]               # None: no such project on CurseForge
    files: List[CfFile] = field(default_factory=list)


def lookup_url() -> str:
    """The service's address, read when a client is made (tests and self-hosting set it)."""
    return os.environ.get("MODKEEL_CURSEFORGE_URL", DEFAULT_URL).rstrip("/")


class CurseForgeClient:
    """Lookups through the service. `available` turns False at the first sign the service is
    off (not configured, not deployed, unreachable): nothing is asked again in this run."""

    def __init__(self, base_url: Optional[str] = None, session=requests):
        self.base_url = lookup_url() if base_url is None else base_url.rstrip("/")
        self.session = session
        self.available = bool(self.base_url)
        self.last_error: Optional[str] = None if self.available else "lookups turned off"
        self._files: Dict[tuple, Optional[CfProject]] = {}

    def _call(self, method: str, path: str, **kwargs) -> Optional[Dict]:
        """The service's JSON answer, or None. An answer meaning "off" (404 before the routes
        are deployed, 502/503 without a working key) or no answer at all turns lookups off;
        any other failure (a refused input) only fails this call."""
        if not self.available:
            return None
        try:
            resp = self.session.request(method, f"{self.base_url}{path}", timeout=TIMEOUT,
                                        headers={"User-Agent": MODRINTH_USER_AGENT}, **kwargs)
        except requests.RequestException as e:
            return self._off(f"unreachable ({e.__class__.__name__})")
        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError:
                return self._off("not a JSON answer")
        try:
            reason = resp.json().get("error", "")
        except (ValueError, AttributeError):
            reason = ""
        reason = reason or f"HTTP {resp.status_code}"
        if resp.status_code in (404, 502, 503):
            return self._off(reason)
        self.last_error = reason
        logger.info("CurseForge lookup %s failed: %s", path, reason)
        return None

    def _off(self, reason: str) -> None:
        logger.info("CurseForge lookups off for this run: %s", reason)
        self.available, self.last_error = False, reason
        return None

    def match(self, paths: Iterable[Path]) -> Dict[Path, CfMatch]:
        """The JARs CurseForge knows by fingerprint. Fingerprints are computed only once the
        service is known to be on, so an off service costs no hashing."""
        paths = list(paths)
        if not paths or not self.available:
            return {}
        by_print: Dict[int, List[Path]] = {}
        for path in paths:
            by_print.setdefault(fingerprint(path), []).append(path)
        prints = list(by_print)
        found: Dict[Path, CfMatch] = {}
        for start in range(0, len(prints), MAX_FINGERPRINTS):
            answer = self._call("POST", "/fingerprints",
                                json={"fingerprints": prints[start:start + MAX_FINGERPRINTS]})
            for m in (answer or {}).get("matches", []):
                match = CfMatch(CfMod.of(m["mod"]), CfFile.of(m["file"]))
                for path in by_print.get(m.get("fingerprint"), []):
                    found[path] = match
        return found

    def files(self, ref: str, mc_version: str, loader: str) -> Optional[CfProject]:
        """A project by id or slug and its files for the target; None when it could not be
        asked (see last_error). Answers are kept for the run: the target layer asks the same
        mod about several versions, and every strategy run may ask again."""
        key = (str(ref).lower(), mc_version, loader.lower())
        if key not in self._files:
            answer = self._call("GET", "/files", params={"mod": key[0], "mc": mc_version,
                                                         "loader": key[2]})
            if answer is None:
                return None
            mod = answer.get("mod")
            self._files[key] = CfProject(CfMod.of(mod) if mod else None,
                                         [CfFile.of(f) for f in answer.get("files", [])])
        return self._files[key]


@lru_cache(maxsize=1)
def default_client() -> CurseForgeClient:
    """One client per run, so "the service is off" is learnt once and every caller shares it."""
    return CurseForgeClient()
