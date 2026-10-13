"""The JDK a mod builds with: the Java its Minecraft version runs on.

Gradle runs on whatever `java` the machine has, and a mod for Minecraft 26.x does not compile
on Java 21. So the build picks a JDK for the target: an installed one of that major (JAVA_HOME,
MODKEEL_HOME/jdks, PATH), else the nearest newer one, else Eclipse Temurin downloaded once from
Adoptium into MODKEEL_HOME/jdks, its SHA-256 checked against Adoptium's.
"""

import hashlib
import logging
import os
import platform
import re
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

import requests

from modkeel.constants import MODKEEL_HOME
from modkeel.core.events import Emitter, Message
from modkeel.core.text import print_event

logger = logging.getLogger("modkeel")

ADOPTIUM = "https://api.adoptium.net/v3/assets/latest/{major}/hotspot"
EXE = ".exe" if os.name == "nt" else ""


def java_for(mc_version: Optional[str]) -> Optional[int]:
    """The Java major a Minecraft release runs on (Mojang's javaVersion), None if unknown."""
    if not mc_version:
        return None
    parts = [int(p) for p in re.findall(r"\d+", mc_version)[:3]]
    if not parts:
        return None
    if parts[0] >= 26:  # 26.1 onward
        return 25
    if parts[0] != 1 or len(parts) < 2:
        return None
    minor, patch = parts[1], parts[2] if len(parts) > 2 else 0
    if (minor, patch) >= (20, 5):
        return 21
    if minor >= 18:
        return 17
    if minor == 17:
        return 16
    return 8


def jdk_major(home: Path) -> int:
    """The major of the JDK at home (its `release` file), 0 when it is no JDK."""
    release = home / "release"
    if not (home / "bin" / f"javac{EXE}").exists() or not release.exists():
        return 0
    m = re.search(r'JAVA_VERSION="(?:1\.)?(\d+)',
                  release.read_text(encoding="utf-8", errors="replace"))
    return int(m.group(1)) if m else 0


def jdks_dir() -> Path:
    return MODKEEL_HOME / "jdks"


def installed() -> List[Path]:
    """JDK homes on this machine Modkeel can see: JAVA_HOME, its own, the one on PATH."""
    found = [os.environ.get("JAVA_HOME")]
    if jdks_dir().is_dir():
        found += [str(p) for p in sorted(jdks_dir().iterdir()) if p.is_dir()]
    javac = shutil.which("javac")
    if javac:
        found.append(str(Path(javac).resolve().parent.parent))
    homes: List[Path] = []
    for h in filter(None, found):
        p = Path(h)
        # an archive unpacked on macOS keeps the home under Contents/Home
        p = p / "Contents" / "Home" if (p / "Contents" / "Home").is_dir() else p
        if p not in homes and jdk_major(p):
            homes.append(p)
    return homes


def pick(major: int, homes: List[Path]) -> Optional[Path]:
    """The exact major if there is one (older Gradle refuses newer JDKs), else the nearest
    newer one."""
    newer = sorted((jdk_major(h), str(h)) for h in homes if jdk_major(h) >= major)
    return Path(newer[0][1]) if newer else None


def _platform() -> Dict[str, str]:
    system = {"windows": "windows", "darwin": "mac", "linux": "linux"}.get(
        platform.system().lower(), "linux")
    machine = platform.machine().lower()
    arch = "aarch64" if machine in ("arm64", "aarch64") else "x64"
    return {"os": system, "architecture": arch}


def download(major: int, events: Emitter = print_event) -> Optional[Path]:
    """Eclipse Temurin JDK `major` into MODKEEL_HOME/jdks; its home, or None on failure."""
    try:
        r = requests.get(ADOPTIUM.format(major=major),
                         params={**_platform(), "image_type": "jdk", "vendor": "eclipse"},
                         timeout=30)
        r.raise_for_status()
        package = r.json()[0]["binary"]["package"]
    except (requests.RequestException, ValueError, LookupError) as e:
        logger.debug("Adoptium lookup failed: %s", e)
        return None
    events(Message(f"    ☕ Downloading Java {major} (Eclipse Temurin, "
                   f"{package['size'] // 2**20} MB) into {jdks_dir()} ..."))
    jdks_dir().mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=jdks_dir()) as tmp:
        archive = Path(tmp) / package["name"]
        digest = hashlib.sha256()
        try:
            with requests.get(package["link"], stream=True, timeout=60) as d:
                d.raise_for_status()
                with open(archive, "wb") as f:
                    for chunk in d.iter_content(1 << 20):
                        f.write(chunk)
                        digest.update(chunk)
        except requests.RequestException as e:
            logger.debug("JDK download failed: %s", e)
            return None
        if digest.hexdigest() != package["checksum"]:
            events(Message(f"    ❌ Java {major} download did not match Adoptium's "
                           "checksum: not used"))
            return None
        unpacked = Path(tmp) / "x"
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as z:
                z.extractall(unpacked)
        else:
            with tarfile.open(archive) as t:
                t.extractall(unpacked, filter="data") if hasattr(tarfile, "data_filter") \
                    else t.extractall(unpacked)
        top = next(unpacked.iterdir())
        dest = jdks_dir() / top.name
        if not dest.exists():
            top.rename(dest)
    home = dest / "Contents" / "Home" if (dest / "Contents" / "Home").is_dir() else dest
    if os.name != "nt":
        for tool in (home / "bin").iterdir():
            tool.chmod(tool.stat().st_mode | 0o111)
    return home if jdk_major(home) else None


def build_env(mc_version: Optional[str], events: Emitter = print_event) -> Optional[dict]:
    """The environment Gradle runs in for this target: JAVA_HOME and PATH on the JDK it
    needs. None keeps the machine's own (target unknown, or nothing better found)."""
    major = java_for(mc_version)
    if not major:
        return None
    home = pick(major, installed()) or download(major, events)
    if not home:
        events(Message(f"    ⚠️  Minecraft {mc_version} builds with Java {major}; "
                       "none found or downloaded, building with the machine's own"))
        return None
    env = dict(os.environ, JAVA_HOME=str(home))
    env["PATH"] = str(home / "bin") + os.pathsep + env.get("PATH", "")
    return env
