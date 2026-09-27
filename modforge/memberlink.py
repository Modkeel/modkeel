"""Member-level linkage: will this JAR hit NoSuchMethodError / NoSuchFieldError at runtime?

``linkage.py`` stops at class existence because Mojang's ProGuard mappings carry no class
hierarchy, and without one every inherited member looks missing. Minecraft 26.x ships
unobfuscated, so the hierarchy can be read straight from the game's own class files. This
module builds a class database from the game jar, the loader, and the mod's dependencies,
then resolves every method and field the mod's bytecode references the way the JVM does:
declared on the owner, else up the superclass chain, else through the interfaces.

A reference that does not resolve is code that throws the moment it runs, which is why a
port can start cleanly and crash an hour into a session: the broken call sits on a path
startup never takes.

What counts as checkable: an owner class whose package belongs to one of the reference
jars (game, loader, dependencies). References into the JDK or into libraries we did not
load are skipped, and so is any lookup whose hierarchy walk leaves the loaded jars, so an
unknown superclass never turns into a false "missing".

Known blind spots, kept visible in the report rather than guessed around:
- NeoForge/Forge patch client classes too, but only their patched *server* jar is on disk.
  Members a patch adds to a client-only class are looked up in the loader's extension
  interfaces; anything still unresolved there is reported separately as ``unverified``.
- Members added by Mixin at runtime (not through Fabric interface injection) are invisible.
"""

import io
import json
import logging
import re
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from modforge.linkage import (TAG_CLASS, TAG_FIELDREF, TAG_INTERFACE_METHODREF, TAG_METHODREF,
                              TAG_NAME_AND_TYPE, ClassFileError, _class_internal_name,
                              _parse_pool, _utf8)

logger = logging.getLogger("modforge")

OBJECT = "java/lang/Object"
OBJECT_METHODS = {"equals", "hashCode", "toString", "getClass", "notify", "notifyAll",
                  "wait", "clone", "finalize"}
NESTED_JAR = re.compile(r"META-INF/(jars|jarjar)/.+\.jar$")
EXTENSION_PACKAGE = re.compile(r"^net/(neoforged/neoforge|minecraftforge)/.*/extensions/")
FOUND, MISSING, UNKNOWN = "found", "missing", "unknown"
# Mixin/ASM: bundled MixinExtras carries shims for older Mixin versions behind runtime
# version checks, so its references into these libraries are deliberately unresolvable.
IGNORED_OWNERS = ("org/spongepowered/", "org/objectweb/")


@dataclass
class ClassInfo:
    name: str
    super_name: Optional[str]
    interfaces: List[str]
    fields: Set[Tuple[str, str]]
    methods: Set[Tuple[str, str]]
    source: str
    # @Mixin targets when this class is a mixin: at runtime they gain its members/interfaces
    mixin_targets: List[str] = field(default_factory=list)


def parse_class_info(data: bytes, source: str = "") -> ClassInfo:
    """Name, superclass, interfaces and declared members of one class file."""
    pool, offset = _parse_pool(data)

    def u2(at: int) -> int:
        if at + 2 > len(data):
            raise ClassFileError("truncated class body")
        return struct.unpack_from(">H", data, at)[0]

    name = _class_internal_name(pool, u2(offset + 2))
    if not name:
        raise ClassFileError("unresolvable this_class")
    super_name = _class_internal_name(pool, u2(offset + 4))
    count = u2(offset + 6)
    interfaces = [i for i in (_class_internal_name(pool, u2(offset + 8 + 2 * k))
                              for k in range(count)) if i]
    offset += 8 + 2 * count

    def members(at: int) -> Tuple[Set[Tuple[str, str]], int]:
        found = set()
        n, at = u2(at), at + 2
        for _ in range(n):
            mname, desc = _utf8(pool, u2(at + 2)), _utf8(pool, u2(at + 4))
            if mname and desc:
                found.add((mname, desc))
            attrs, at = u2(at + 6), at + 8
            for _ in range(attrs):
                if at + 6 > len(data):
                    raise ClassFileError("truncated attribute")
                at += 6 + struct.unpack_from(">I", data, at + 2)[0]
        return found, at

    fields, offset = members(offset)
    methods, offset = members(offset)
    targets: List[str] = []
    for _ in range(u2(offset)):
        attr_name, length = _utf8(pool, u2(offset + 2)), struct.unpack_from(">I", data, offset + 4)[0]
        if attr_name in ("RuntimeInvisibleAnnotations", "RuntimeVisibleAnnotations"):
            targets += _mixin_targets(data[offset + 8:offset + 8 + length], pool)
        offset += 6 + length
    return ClassInfo(name, super_name, interfaces, fields, methods, source, targets)


MIXIN_ANNOTATION = "Lorg/spongepowered/asm/mixin/Mixin;"


def _mixin_targets(attr: bytes, pool) -> List[str]:
    """Target classes of an @Mixin annotation: class literals in ``value``, strings in
    ``targets``. Every other annotation is skipped without being interpreted."""
    targets: List[str] = []

    def u2(at: int) -> int:
        return struct.unpack_from(">H", attr, at)[0]

    def element(at: int, collect: bool) -> int:
        tag = chr(attr[at])
        at += 1
        if tag in "BCDFIJSZs":
            if collect and tag == "s":
                value = _utf8(pool, u2(at))
                if value:
                    targets.append(value.replace(".", "/"))
            return at + 2
        if tag == "e":
            return at + 4
        if tag == "c":
            if collect:
                desc = _utf8(pool, u2(at)) or ""
                if desc.startswith("L") and desc.endswith(";"):
                    targets.append(desc[1:-1])
            return at + 2
        if tag == "@":
            return annotation(at, False)
        if tag == "[":
            n, at = u2(at), at + 2
            for _ in range(n):
                at = element(at, collect)
            return at
        raise ClassFileError(f"bad element tag {tag!r}")

    def annotation(at: int, top: bool) -> int:
        is_mixin = top and _utf8(pool, u2(at)) == MIXIN_ANNOTATION
        n, at = u2(at + 2), at + 4
        for _ in range(n):
            key = _utf8(pool, u2(at))
            at = element(at + 2, is_mixin and key in ("value", "targets"))
        return at

    try:
        at = 2
        for _ in range(u2(0)):
            at = annotation(at, True)
    except (struct.error, IndexError, ClassFileError):
        pass
    return targets


def member_refs(data: bytes) -> Tuple[str, Set[Tuple[str, str, str, str]]]:
    """(this class, {(kind, owner, name, descriptor)}) for every field/method reference, plus
    ("class", name, "", "") for every class the pool names (types, supertypes, casts,
    class literals): a missing one is a NoClassDefFoundError."""
    pool, offset = _parse_pool(data)
    this = _class_internal_name(pool, struct.unpack_from(">H", data, offset + 2)[0]) or ""
    refs = set()
    for i, entry in enumerate(pool):
        if entry and entry[0] == TAG_CLASS:
            name = _class_internal_name(pool, i)
            if name:
                name = name.lstrip("[")
                if name.startswith("L") and name.endswith(";"):
                    name = name[1:-1]
                if "/" in name:
                    refs.add(("class", name, "", ""))
            continue
        if not entry or entry[0] not in (TAG_FIELDREF, TAG_METHODREF, TAG_INTERFACE_METHODREF):
            continue
        class_index, nat_index = struct.unpack(">HH", entry[1])
        owner = _class_internal_name(pool, class_index)
        nat = pool[nat_index] if 0 < nat_index < len(pool) else None
        if not owner or owner.startswith("[") or not nat or nat[0] != TAG_NAME_AND_TYPE:
            continue
        name_index, desc_index = struct.unpack(">HH", nat[1])
        name, desc = _utf8(pool, name_index), _utf8(pool, desc_index)
        if name and desc:
            refs.add(("field" if entry[0] == TAG_FIELDREF else "method", owner, name, desc))
    return this, refs


def _package(internal: str) -> str:
    return internal.rsplit("/", 1)[0] if "/" in internal else ""


def _iter_jar_classes(archive: zipfile.ZipFile, label: str, depth: int = 0):
    """(label, class bytes) for a jar and, recursively, the jars nested inside it."""
    for info in archive.infolist():
        n = info.filename
        if n.endswith(".class") and not n.startswith("META-INF/versions/"):
            yield label, archive.read(info), None
        elif n in ("fabric.mod.json", "META-INF/interfaces.json"):
            yield label, None, archive.read(info)
        elif depth < 3 and NESTED_JAR.match(n):
            try:
                with zipfile.ZipFile(io.BytesIO(archive.read(info))) as inner:
                    yield from _iter_jar_classes(inner, f"{label}!{n.rsplit('/', 1)[-1]}",
                                                 depth + 1)
            except zipfile.BadZipFile:
                continue


@dataclass
class ClassDB:
    """Every class of the reference jars, by internal name. First jar added wins, so add
    patched game jars before vanilla ones."""

    classes: Dict[str, ClassInfo] = field(default_factory=dict)
    packages: Set[str] = field(default_factory=set)
    # Fabric interface injection: target class -> interfaces added at load time
    injected: Dict[str, Set[str]] = field(default_factory=dict)

    def add_jar(self, jar: Path, label: Optional[str] = None, track_packages: bool = True
                ) -> int:
        added = 0
        with zipfile.ZipFile(jar) as archive:
            for src, data, fmj in _iter_jar_classes(archive, label or jar.name):
                if fmj is not None:
                    self._read_injected(fmj)
                    continue
                try:
                    info = parse_class_info(data, src)
                except (ClassFileError, struct.error):
                    continue
                if info.name in self.classes:
                    continue
                self.classes[info.name] = info
                self.add_mixin(info)
                if track_packages:
                    self.packages.add(_package(info.name))
                added += 1
        return added

    def add_mixin(self, info: ClassInfo) -> None:
        """A mixin's interfaces and members land on its targets at runtime: model that as
        the target implementing the mixin class itself."""
        for target in info.mixin_targets:
            self.injected.setdefault(target, set()).add(info.name)

    def _read_injected(self, raw: bytes) -> None:
        try:
            meta = json.loads(raw.decode("utf-8-sig", "replace"), strict=False)
        except ValueError:
            return
        if not isinstance(meta, dict):
            return
        if "schemaVersion" in meta or "id" in meta:
            # fabric.mod.json: Loom interface injection
            mapping = (meta.get("custom") or {}).get("loom:injected_interfaces") or {}
        else:
            # NeoForge META-INF/interfaces.json: {target: [interfaces]}
            mapping = meta
        for target, ifaces in mapping.items():
            if isinstance(ifaces, list):
                self.injected.setdefault(target, set()).update(ifaces)

    def resolve(self, kind: str, owner: str, name: str, desc: str,
                overlay: Optional["ClassDB"] = None) -> str:
        """found / missing / unknown, walking supers and interfaces like the JVM.
        ``overlay`` holds the mod's own classes and the interfaces it injects."""
        seen: Set[str] = set()
        stack = [owner]
        unknown = False
        while stack:
            cls = stack.pop()
            if cls in seen:
                continue
            seen.add(cls)
            if cls == OBJECT:
                if kind == "method" and name in OBJECT_METHODS:
                    return FOUND
                continue
            info = self.classes.get(cls) or (overlay.classes.get(cls) if overlay else None)
            if info is None:
                unknown = True
                continue
            members = info.fields if kind == "field" else info.methods
            if (name, desc) in members:
                return FOUND
            # Signature-polymorphic and varargs quirks do not exist outside the JDK, so an
            # exact (name, descriptor) match is what the JVM itself requires.
            if info.super_name:
                stack.append(info.super_name)
            stack.extend(info.interfaces)
            stack.extend(self.injected.get(cls, ()))
            if overlay:
                stack.extend(overlay.injected.get(cls, ()))
        return UNKNOWN if unknown else MISSING

    def in_extension(self, kind: str, name: str, desc: str) -> bool:
        """Declared by any loader extension interface (NeoForge/Forge patch helpers)."""
        if kind != "method":
            return False
        return any((name, desc) in c.methods for n, c in self.classes.items()
                   if EXTENSION_PACKAGE.match(n))


@dataclass
class MemberReport:
    jar: str
    refs_checked: int = 0
    missing_classes: List[str] = field(default_factory=list)
    missing_members: List[str] = field(default_factory=list)
    unverified: List[str] = field(default_factory=list)
    unknown: int = 0
    # which of the mod's own classes hold each finding, for triage
    sites: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def findings(self) -> int:
        return len(self.missing_classes) + len(self.missing_members)


def check_jar_members(jar: Path, db: ClassDB, patched_only: Optional[Set[str]] = None
                      ) -> MemberReport:
    """Resolve every reference the jar (and its nested jars) makes into the reference jars.

    ``patched_only``: classes the loader patched and we have the patched copy of; a
    miss on any *other* game class for a patching loader is ``unverified``, not missing.
    """
    report = MemberReport(jar=Path(jar).name)
    own: Set[str] = set()
    overlay = ClassDB()
    refs_by_site: Dict[Tuple[str, str, str, str], Set[str]] = {}
    with zipfile.ZipFile(jar) as archive:
        for src, data, fmj in _iter_jar_classes(archive, Path(jar).name):
            if data is None:
                overlay._read_injected(fmj)
                continue
            try:
                this, refs = member_refs(data)
                info = parse_class_info(data, src)
                overlay.classes.setdefault(this, info)
                overlay.add_mixin(info)
            except (ClassFileError, struct.error):
                continue
            own.add(this)
            if this in db.classes:
                # A bundled copy of a library the dependencies already provide: the loader
                # keeps one (Fabric the newest), so this copy's references never run.
                continue
            for ref in refs:
                refs_by_site.setdefault(ref, set()).add(this)

    missing_classes: Set[str] = set()
    for ref, sites in sorted(refs_by_site.items()):
        kind, owner, name, desc = ref
        if owner in own or owner.startswith(IGNORED_OWNERS) \
                or _package(owner) not in db.packages:
            continue
        report.refs_checked += 1
        if owner not in db.classes:
            if owner not in missing_classes:
                missing_classes.add(owner)
                report.sites[owner] = sorted(sites)[:3]
            continue
        if kind == "class":
            continue
        verdict = db.resolve(kind, owner, name, desc, overlay)
        if verdict == FOUND:
            continue
        if verdict == UNKNOWN:
            report.unknown += 1
            continue
        label = f"{kind} {owner}.{name}{desc if kind == 'method' else ':' + desc}"
        if db.in_extension(kind, name, desc):
            continue
        if patched_only is not None and owner not in patched_only \
                and owner.startswith(("net/minecraft/", "com/mojang/")):
            report.unverified.append(label)
            continue
        report.missing_members.append(label)
        report.sites[label] = sorted(sites)[:3]
    report.missing_classes = sorted(missing_classes)
    return report


JDK_CLASSES = Path.home() / ".modforge" / "jdk-classes"


def add_jdk(db: ClassDB) -> bool:
    """Add java.base so enums, records and JDK supertypes resolve instead of stopping the
    walk as unknown. Built once with ``jimage extract`` (see research/member_check.py)."""
    jars = sorted(JDK_CLASSES.glob("java.base-*.jar"))
    if jars:
        db.add_jar(jars[-1], track_packages=False)
    return bool(jars)


def build_db(game_jars: Iterable[Path], other_jars: Iterable[Path]) -> ClassDB:
    """Reference database: game jars first (patched before vanilla), then loader + deps."""
    db = ClassDB()
    add_jdk(db)
    for jar in game_jars:
        db.add_jar(Path(jar))
    for jar in other_jars:
        db.add_jar(Path(jar))
    return db
