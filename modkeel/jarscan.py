"""What a mod JAR can do outside the game, read from its bytecode.

A mod is Java with the player's own rights: files, network, other programs. Minecraft has no
sandbox for it. fractureiser (June 2023) shipped inside mods on CurseForge and Bukkit and
stole browser cookies, Discord tokens and Minecraft sessions: a few lines in a real mod that
downloaded a class from a server and ran it.

``scan_jar`` reads every class's constant pool (the classes, methods and strings it uses,
without running anything), nested JARs included (Fabric ``META-INF/jars``, Forge
``META-INF/jarjar``), and reports:

- ``high``: what only malware does. A known family's classes or servers; chat webhooks a
  stealer posts to; the files where browsers, Discord and launchers keep logins, in a JAR
  that also talks to the network.
- ``medium``: what malware needs but some honest mods do too. Code loaded from bytes or from
  a URL in a class that also downloads; starting other programs; a long encoded blob in a
  class that decodes it. An updater or a library loader does these for good reasons, so a
  medium finding asks who published the JAR (see the caller's policy), not a verdict.

Static reading is a filter, not a proof: reflection and encrypted strings can hide a call.
It catches what known Minecraft malware did and makes hiding cost something.
"""

import io
import re
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Set, Tuple

from modkeel.linkage import ClassFileError, _class_internal_name, _parse_pool, _utf8

TAG_CLASS = 7
TAG_STRING = 8
TAG_METHODREF = (10, 11)
TAG_NAME_AND_TYPE = 12

HIGH = "high"
MEDIUM = "medium"

NESTED = ("META-INF/jars/", "META-INF/jarjar/", "META-INF/jarinjar/")
MAX_NESTED_DEPTH = 3
MAX_CLASS_BYTES = 8 * 2**20

# known families: class packages and servers they used (public indicators)
KNOWN_CLASSES = re.compile(r"^dev/neko/(nekoclient|nekoinjector)/")
KNOWN_STRINGS = re.compile(r"85\.217\.144\.130|107\.189\.3\.101|files\.skyrage\.de|skyrage\.de")
# where a stealer sends what it took
EXFIL = re.compile(r"(?i)(discord(app)?\.com/api/webhooks/|api\.telegram\.org/bot|"
                   r"canary\.discord\.com/api/webhooks/)")
# where browsers, Discord and launchers keep logins
CREDENTIALS = re.compile(r"(?i)(Local Storage[\\/]+leveldb|[\\/]Login Data\b|[\\/]Local State\b|"
                         r"cookies\.sqlite|key4\.db|logins\.json|launcher_accounts"
                         r"(_microsoft_store)?\.json|[\\/]\.feather[\\/]+accounts|"
                         r"lunarclient[\\/]+settings[\\/]+game[\\/]+accounts|"
                         r"Exodus[\\/]+exodus\.wallet)")
BASE64_BLOB = re.compile(r"^[A-Za-z0-9+/=_-]{4000,}$")

NETWORK = {("java/net/URL", "openConnection"), ("java/net/URL", "openStream"),
           ("java/net/http/HttpClient", "send"), ("java/net/http/HttpClient", "sendAsync"),
           ("java/net/Socket", "<init>"), ("java/net/Socket", "getOutputStream"),
           ("java/net/URI", "toURL")}
DEFINE = {("java/lang/ClassLoader", "defineClass"),
          ("java/lang/invoke/MethodHandles$Lookup", "defineClass"),
          ("java/lang/invoke/MethodHandles$Lookup", "defineHiddenClass"),
          ("sun/misc/Unsafe", "defineClass"), ("sun/misc/Unsafe", "defineAnonymousClass"),
          ("java/security/SecureClassLoader", "defineClass")}
URL_LOADER = {("java/net/URLClassLoader", "<init>"), ("java/net/URLClassLoader", "newInstance")}
EXEC = {("java/lang/Runtime", "exec"), ("java/lang/ProcessBuilder", "start")}
DECODE = {("java/util/Base64$Decoder", "decode")}


@dataclass
class Finding:
    level: str
    kind: str
    where: str
    detail: str = ""

    def line(self) -> str:
        tail = f": {self.detail}" if self.detail else ""
        return f"{self.level} {self.kind} in {self.where}{tail}"


@dataclass
class ScanReport:
    path: str
    classes: int = 0
    findings: List[Finding] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def level(self) -> Optional[str]:
        levels = {f.level for f in self.findings}
        return HIGH if HIGH in levels else MEDIUM if MEDIUM in levels else None


@dataclass
class _ClassUse:
    name: str
    methods: Set[Tuple[str, str]]
    strings: List[str]
    classes: Set[str]


def read_class(data: bytes) -> Optional[_ClassUse]:
    """The methods, strings and classes one class file uses (its constant pool)."""
    try:
        pool, after = _parse_pool(data)
    except ClassFileError:
        return None
    methods: Set[Tuple[str, str]] = set()
    strings: List[str] = []
    classes: Set[str] = set()
    for entry in pool:
        if entry is None:
            continue
        tag, payload = entry
        if tag == TAG_CLASS:
            name = _utf8(pool, struct.unpack(">H", payload)[0])
            if name:
                classes.add(name)
        elif tag == TAG_STRING:
            s = _utf8(pool, struct.unpack(">H", payload)[0])
            if s:
                strings.append(s)
        elif tag in TAG_METHODREF:
            owner_index, nat_index = struct.unpack(">HH", payload)
            owner = _class_internal_name(pool, owner_index)
            nat = pool[nat_index] if 0 < nat_index < len(pool) else None
            if owner and nat and nat[0] == TAG_NAME_AND_TYPE:
                name = _utf8(pool, struct.unpack(">HH", nat[1])[0])
                if name:
                    methods.add((owner, name))
    this = ""
    if after + 4 <= len(data):
        this = _class_internal_name(pool, struct.unpack_from(">H", data, after + 2)[0]) or ""
    return _ClassUse(this, methods, strings, classes)


def _classes(z: zipfile.ZipFile, prefix: str, depth: int) -> Iterator[Tuple[str, bytes]]:
    for info in z.infolist():
        n = info.filename
        if info.file_size > MAX_CLASS_BYTES:
            continue
        if n.endswith(".class"):
            yield prefix + n, z.read(info)
        elif n.endswith(".jar") and n.startswith(NESTED) and depth < MAX_NESTED_DEPTH:
            try:
                with zipfile.ZipFile(io.BytesIO(z.read(info))) as inner:
                    yield from _classes(inner, f"{prefix}{n}!/", depth + 1)
            except zipfile.BadZipFile:
                continue


def scan_jar(path: Path) -> ScanReport:
    report = ScanReport(str(path))
    try:
        with zipfile.ZipFile(path) as z:
            uses = []
            for where, data in _classes(z, "", 0):
                use = read_class(data)
                if use:
                    use.name = use.name or where
                    uses.append(use)
    except (zipfile.BadZipFile, OSError) as e:
        report.error = str(e)
        return report
    report.classes = len(uses)
    report.findings = judge(uses)
    return report


def judge(uses: List[_ClassUse]) -> List[Finding]:
    found: List[Finding] = []
    jar_network = any(u.methods & NETWORK for u in uses)
    for u in uses:
        known = [c for c in u.classes if KNOWN_CLASSES.match(c)]
        if KNOWN_CLASSES.match(u.name) or known:
            found.append(Finding(HIGH, "known-malware", u.name, (known or [u.name])[0]))
        for s in u.strings:
            if KNOWN_STRINGS.search(s):
                found.append(Finding(HIGH, "known-malware", u.name, _short(s)))
            elif EXFIL.search(s):
                found.append(Finding(HIGH, "exfiltration", u.name, _short(s)))
            elif CREDENTIALS.search(s):
                found.append(Finding(HIGH if jar_network else MEDIUM, "credentials", u.name,
                                     _short(s)))
        network = bool(u.methods & NETWORK)
        if network and (u.methods & DEFINE or u.methods & URL_LOADER):
            found.append(Finding(MEDIUM, "remote-code", u.name,
                                 "downloads and loads classes in one place"))
        if u.methods & EXEC:
            found.append(Finding(MEDIUM, "runs-programs", u.name,
                                 ", ".join(sorted(f"{o.rsplit('/', 1)[-1]}.{m}"
                                                  for o, m in u.methods & EXEC))))
        if u.methods & DECODE and u.methods & DEFINE and any(BASE64_BLOB.match(s)
                                                             for s in u.strings):
            found.append(Finding(MEDIUM, "hidden-code", u.name, "decodes a blob into a class"))
    return found


def _short(s: str, n: int = 120) -> str:
    return s if len(s) <= n else s[:n] + "..."
