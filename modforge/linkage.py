"""Bytecode linkage checking for already-compiled JARs (Level 0.5).

Level 0 downloads prebuilt JARs from GitHub Releases and CI artifacts, and a JAR's
metadata can claim any Minecraft version it likes. This module reads what the bytecode
actually references and resolves it against the target version's symbol table, so a JAR
built for the wrong version is caught before it reaches the user's mods folder.

Only the constant pool is parsed -- every external reference a class makes is listed
there, so nothing past it needs decoding. That keeps this to a few hundred lines of pure
Python with no bytecode library.

Two limits, both found by running against real JARs rather than reasoned about in advance:

**Class-level only.** Bytecode records the *static receiver type* of every call, which is
frequently a subclass that inherits the member rather than declaring it. Resolving that
needs the class hierarchy, and Mojang's ProGuard mappings do not contain one -- they list
each class's own members and nothing about its supertypes. Checking JEI 1.21.1 against its
own version produced 97 phantom "missing methods", almost all inherited
(``Screen.mouseClicked`` from ``GuiEventListener``, ``EditBox.isFocused`` from
``AbstractWidget``) and a handful added by NeoForge's patches to vanilla classes. Class
existence has no such problem: the same JAR reports zero missing classes against 1.21.1 and
thirteen genuine ones against 1.20.1. Methods and fields are still extracted -- SRG
detection needs their names -- but never checked.

**Naming scheme.** Production JARs are not always in Mojang names: Fabric mods ship
remapped to intermediary, Forge and pre-1.20.2 NeoForge to SRG. ``detect_naming_scheme``
refuses to check what it cannot interpret rather than declaring everything missing.
"""

import logging
import re
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set, Tuple

from modforge.symbols import SymbolIndex, is_candidate

logger = logging.getLogger("modforge")

CLASS_FILE_MAGIC = b"\xca\xfe\xba\xbe"

TAG_UTF8 = 1
TAG_CLASS = 7
TAG_FIELDREF = 9
TAG_METHODREF = 10
TAG_INTERFACE_METHODREF = 11
TAG_NAME_AND_TYPE = 12
TAG_LONG = 5
TAG_DOUBLE = 6

# Byte width of every constant pool entry that is not CONSTANT_Utf8 (which is
# length-prefixed and handled separately).
FIXED_ENTRY_SIZES = {
    3: 4,  # Integer
    4: 4,  # Float
    5: 8,  # Long
    6: 8,  # Double
    7: 2,  # Class
    8: 2,  # String
    9: 4,  # Fieldref
    10: 4,  # Methodref
    11: 4,  # InterfaceMethodref
    12: 4,  # NameAndType
    15: 3,  # MethodHandle
    16: 2,  # MethodType
    17: 4,  # Dynamic
    18: 4,  # InvokeDynamic
    19: 2,  # Module
    20: 2,  # Package
}

# Fabric production JARs are remapped to intermediary names; Forge/older NeoForge JARs
# carry SRG member names. Neither can be resolved against Mojang mappings.
INTERMEDIARY_CLASS_RE = re.compile(r"^net/minecraft/class_\d+")
SRG_MEMBER_RE = re.compile(r"^(?:m|f|p)_\d+_$")

# Above this share of unresolvable references, the JAR is in a naming scheme we cannot
# read rather than genuinely broken. Same guard as the source-level check.
MISNAMING_THRESHOLD = 0.5

SCHEME_MOJANG = "mojang"
SCHEME_INTERMEDIARY = "intermediary"
SCHEME_SRG = "srg"


class ClassFileError(ValueError):
    """Raised when a .class file cannot be parsed."""


@dataclass
class ClassRefs:
    """External references made by compiled bytecode."""

    classes: Set[str] = field(default_factory=set)
    # (owner, name, descriptor)
    methods: Set[Tuple[str, str, str]] = field(default_factory=set)
    fields: Set[Tuple[str, str, str]] = field(default_factory=set)
    classes_parsed: int = 0

    def merge(self, other: "ClassRefs") -> None:
        self.classes |= other.classes
        self.methods |= other.methods
        self.fields |= other.fields
        self.classes_parsed += other.classes_parsed


def internal_to_fqcn(internal: str) -> Optional[str]:
    """Convert a JVM internal class name to a dotted FQCN.

    Handles array descriptors (``[Lnet/minecraft/core/BlockPos;``) and returns None for
    primitive arrays, which name no class.
    """
    name = internal
    while name.startswith("["):
        name = name[1:]
    if name.startswith("L") and name.endswith(";"):
        name = name[1:-1]
    elif len(name) <= 1:
        return None  # primitive array element
    return name.replace("/", ".")


def parse_constant_pool(data: bytes) -> List[Optional[Tuple[int, object]]]:
    """Parse a class file's constant pool. Everything after it is left unread."""
    if len(data) < 10 or data[:4] != CLASS_FILE_MAGIC:
        raise ClassFileError("not a class file")

    count = struct.unpack_from(">H", data, 8)[0]
    pool: List[Optional[Tuple[int, object]]] = [None] * count
    offset = 10
    index = 1

    while index < count:
        if offset >= len(data):
            raise ClassFileError("truncated constant pool")

        tag = data[offset]
        offset += 1

        if tag == TAG_UTF8:
            if offset + 2 > len(data):
                raise ClassFileError("truncated utf8 length")
            length = struct.unpack_from(">H", data, offset)[0]
            offset += 2
            if offset + length > len(data):
                raise ClassFileError("truncated utf8 payload")
            pool[index] = (
                tag,
                data[offset : offset + length].decode("utf-8", "replace"),
            )
            offset += length
        else:
            size = FIXED_ENTRY_SIZES.get(tag)
            if size is None:
                raise ClassFileError(f"unknown constant pool tag {tag}")
            if offset + size > len(data):
                raise ClassFileError("truncated constant pool entry")
            pool[index] = (tag, data[offset : offset + size])
            offset += size

        # Longs and doubles occupy two constant pool slots. The JVM spec calls this a
        # historical mistake; it still has to be honoured.
        index += 2 if tag in (TAG_LONG, TAG_DOUBLE) else 1

    return pool


def _utf8(pool, index: int) -> Optional[str]:
    if index <= 0 or index >= len(pool):
        return None
    entry = pool[index]
    return entry[1] if entry and entry[0] == TAG_UTF8 else None


def _class_internal_name(pool, index: int) -> Optional[str]:
    if index <= 0 or index >= len(pool):
        return None
    entry = pool[index]
    if not entry or entry[0] != TAG_CLASS:
        return None
    return _utf8(pool, struct.unpack(">H", entry[1])[0])


def extract_refs(data: bytes) -> ClassRefs:
    """Extract every external class, method and field reference from one class file."""
    pool = parse_constant_pool(data)
    refs = ClassRefs(classes_parsed=1)

    for entry in pool:
        if entry is None:
            continue
        tag, payload = entry

        if tag == TAG_CLASS:
            internal = _utf8(pool, struct.unpack(">H", payload)[0])
            if internal:
                fqcn = internal_to_fqcn(internal)
                if fqcn:
                    refs.classes.add(fqcn)

        elif tag in (TAG_FIELDREF, TAG_METHODREF, TAG_INTERFACE_METHODREF):
            class_index, nat_index = struct.unpack(">HH", payload)
            internal = _class_internal_name(pool, class_index)
            if not internal:
                continue
            owner = internal_to_fqcn(internal)
            if not owner:
                continue

            nat = pool[nat_index] if 0 < nat_index < len(pool) else None
            if not nat or nat[0] != TAG_NAME_AND_TYPE:
                continue
            name_index, desc_index = struct.unpack(">HH", nat[1])
            name = _utf8(pool, name_index)
            descriptor = _utf8(pool, desc_index)
            if not name or not descriptor:
                continue

            target = refs.fields if tag == TAG_FIELDREF else refs.methods
            target.add((owner, name, descriptor))

    return refs


def detect_naming_scheme(refs: ClassRefs) -> str:
    """Determine which naming scheme a JAR's bytecode is in.

    Fabric production JARs use intermediary class names; Forge and pre-1.20.2 NeoForge
    JARs use SRG member names. Checking either against Mojang mappings would report
    everything as missing.
    """
    for fqcn in refs.classes:
        if INTERMEDIARY_CLASS_RE.match(fqcn.replace(".", "/")):
            return SCHEME_INTERMEDIARY

    for _owner, name, _descriptor in refs.methods | refs.fields:
        if SRG_MEMBER_RE.match(name):
            return SCHEME_SRG

    return SCHEME_MOJANG


@dataclass
class LinkageReport:
    """Outcome of resolving a JAR's bytecode references against a symbol table."""

    checked: bool = True
    skip_reason: Optional[str] = None
    mc_version: Optional[str] = None
    scheme: Optional[str] = None

    missing_classes: List[str] = field(default_factory=list)

    classes_parsed: int = 0
    refs_checked: int = 0

    @classmethod
    def skipped(cls, reason: str, scheme: Optional[str] = None) -> "LinkageReport":
        return cls(checked=False, skip_reason=reason, scheme=scheme)

    @property
    def is_clean(self) -> bool:
        return self.checked and not self.findings

    @property
    def findings(self) -> List[str]:
        return [f"class not in {self.mc_version}: {c}" for c in self.missing_classes]

    @property
    def summary(self) -> str:
        if not self.checked:
            return f"linkage check skipped ({self.skip_reason})"
        if self.is_clean:
            return (
                f"{self.refs_checked} Minecraft references in {self.classes_parsed} "
                f"classes resolve against {self.mc_version}"
            )
        return f"{len(self.missing_classes)} missing classes"


def read_jar_refs(jar_path: Path, max_classes: int = 6000) -> ClassRefs:
    """Read every class in a JAR and merge their references.

    Multi-release entries under ``META-INF/versions/`` are skipped: they are alternate
    implementations for other JDKs and duplicate what the base entries already say.
    """
    merged = ClassRefs()

    with zipfile.ZipFile(jar_path) as archive:
        for info in archive.infolist():
            if merged.classes_parsed >= max_classes:
                logger.debug("read_jar_refs hit max_classes=%d", max_classes)
                break
            name = info.filename
            if not name.endswith(".class") or name.startswith("META-INF/versions/"):
                continue
            try:
                merged.merge(extract_refs(archive.read(info)))
            except (ClassFileError, struct.error, zipfile.BadZipFile) as e:
                logger.debug("skipping %s: %s", name, e)

    return merged


def check_refs(refs: ClassRefs, index: SymbolIndex) -> LinkageReport:
    """Resolve extracted references against a symbol table."""
    scheme = detect_naming_scheme(refs)
    if scheme != SCHEME_MOJANG:
        return LinkageReport.skipped(
            f"JAR uses {scheme} names, not Mojang names", scheme=scheme
        )

    report = LinkageReport(
        mc_version=index.mc_version, scheme=scheme, classes_parsed=refs.classes_parsed
    )

    checkable_classes = sorted(
        c for c in refs.classes if is_candidate(c) and index.covers(c)
    )
    if not checkable_classes:
        return LinkageReport.skipped("no checkable Minecraft references", scheme=scheme)

    missing_classes = [c for c in checkable_classes if not index.has_class(c)]

    report.refs_checked = len(checkable_classes)
    total_missing = len(missing_classes)

    # Wholesale absence means the JAR is in a naming scheme we misread, not that it
    # references thousands of nonexistent symbols.
    if (
        report.refs_checked
        and total_missing / report.refs_checked > MISNAMING_THRESHOLD
    ):
        return LinkageReport.skipped(
            f"{total_missing}/{report.refs_checked} references unresolved "
            f"-- assuming an unreadable naming scheme, not breakage",
            scheme=scheme,
        )

    report.missing_classes = missing_classes
    return report


def check_jar(jar_path: Path, index: SymbolIndex) -> LinkageReport:
    """Read a JAR and resolve its Minecraft references. Never raises."""
    try:
        refs = read_jar_refs(Path(jar_path))
    except (zipfile.BadZipFile, OSError) as e:
        return LinkageReport.skipped(f"unreadable JAR: {e}")

    if refs.classes_parsed == 0:
        return LinkageReport.skipped("JAR contains no class files")

    return check_refs(refs, index)
