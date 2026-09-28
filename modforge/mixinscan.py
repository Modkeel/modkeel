"""What a mod changes in the game, read from its mixins.

Every ``@Mixin`` class names the game classes it targets, and each of its injector methods
names the target method (``@Inject(method = "render")``, ``@Redirect``, ``@ModifyArg``,
MixinExtras' ``@WrapOperation``...). ``@Overwrite`` replaces the method of the same name.
Read from the bytecode alone (annotations survive compilation as RuntimeInvisible
annotations), this gives a map of which game systems a mod touches and how hard: an
``@Overwrite`` or ``@Redirect`` replaces behaviour, an ``@Inject`` adds to it.

Two mods whose injectors land on the same target method are the usual shape of a mixin
conflict: two overwrites of one method cannot both apply, and a redirect of a call another
mod also redirects fails to apply.
"""

import io
import re
import struct
import zipfile
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

from modforge.linkage import ClassFileError, _class_internal_name, _parse_pool, _utf8

MIXIN = "Lorg/spongepowered/asm/mixin/Mixin;"
OVERWRITE = "Lorg/spongepowered/asm/mixin/Overwrite;"
INJECTOR_PACKAGES = ("Lorg/spongepowered/asm/mixin/injection/",
                     "Lcom/llamalad7/mixinextras/injector/")
NESTED_JAR = re.compile(r"META-INF/(jars|jarjar)/.+\.jar$")
# injectors that replace behaviour rather than add to it
REPLACING = {"Overwrite", "Redirect", "WrapOperation", "WrapWithCondition", "ModifyConstant"}


@dataclass
class Injection:
    kind: str           # simple annotation name: Inject, Redirect, Overwrite, ...
    methods: List[str]  # target method selectors as written ("render", "tick()V", ...)


@dataclass
class MixinClass:
    name: str
    targets: List[str]
    injections: List[Injection] = field(default_factory=list)


def _annotations(attr: bytes, pool) -> List[Tuple[str, Dict[str, List[str]]]]:
    """Every top-level annotation as (type descriptor, {element: string/class values})."""
    out: List[Tuple[str, Dict[str, List[str]]]] = []

    def u2(at: int) -> int:
        return struct.unpack_from(">H", attr, at)[0]

    def element(at: int, sink: Optional[List[str]]) -> int:
        tag = chr(attr[at])
        at += 1
        if tag in "BCDFIJSZs":
            if sink is not None and tag == "s":
                sink.append(_utf8(pool, u2(at)) or "")
            return at + 2
        if tag == "e":
            return at + 4
        if tag == "c":
            if sink is not None:
                desc = _utf8(pool, u2(at)) or ""
                sink.append(desc[1:-1] if desc.startswith("L") and desc.endswith(";")
                            else desc)
            return at + 2
        if tag == "@":
            return annotation(at, None)
        if tag == "[":
            n, at = u2(at), at + 2
            for _ in range(n):
                at = element(at, sink)
            return at
        raise ClassFileError(f"bad element tag {tag!r}")

    def annotation(at: int, into: Optional[list]) -> int:
        kind = _utf8(pool, u2(at)) or ""
        n, at = u2(at + 2), at + 4
        values: Dict[str, List[str]] = {}
        for _ in range(n):
            key = _utf8(pool, u2(at)) or ""
            sink = values.setdefault(key, []) if into is not None else None
            at = element(at + 2, sink)
        if into is not None:
            into.append((kind, values))
        return at

    try:
        at = 2
        for _ in range(u2(0)):
            at = annotation(at, out)
    except (struct.error, IndexError, ClassFileError):
        pass
    return out


def _simple(desc: str) -> str:
    return desc.rsplit("/", 1)[-1].rstrip(";")


def parse_mixin(data: bytes) -> Optional[MixinClass]:
    """The mixin's targets and injectors, or None when the class is not a mixin."""
    if MIXIN.encode() not in data:
        return None
    pool, offset = _parse_pool(data)

    def u2(at: int) -> int:
        if at + 2 > len(data):
            raise ClassFileError("truncated class body")
        return struct.unpack_from(">H", data, at)[0]

    def u4(at: int) -> int:
        if at + 4 > len(data):
            raise ClassFileError("truncated class body")
        return struct.unpack_from(">I", data, at)[0]

    name = _class_internal_name(pool, u2(offset + 2)) or ""
    offset += 8 + 2 * u2(offset + 6)

    def annotated(at: int, attrs: int) -> Tuple[List[Tuple[str, Dict[str, List[str]]]], int]:
        found = []
        for _ in range(attrs):
            attr_name, length = _utf8(pool, u2(at)), u4(at + 2)
            if attr_name in ("RuntimeInvisibleAnnotations", "RuntimeVisibleAnnotations"):
                found += _annotations(data[at + 6:at + 6 + length], pool)
            at += 6 + length
        return found, at

    # fields: skip
    n, offset = u2(offset), offset + 2
    for _ in range(n):
        _, offset = annotated(offset + 8, u2(offset + 6))
    injections: List[Injection] = []
    n, offset = u2(offset), offset + 2
    for _ in range(n):
        method_name = _utf8(pool, u2(offset + 2)) or ""
        anns, offset = annotated(offset + 8, u2(offset + 6))
        for kind, values in anns:
            if kind == OVERWRITE:
                injections.append(Injection("Overwrite", [method_name]))
            elif kind.startswith(INJECTOR_PACKAGES) and "method" in values:
                injections.append(Injection(_simple(kind), values["method"]))
    targets: List[str] = []
    class_anns, _ = annotated(offset + 2, u2(offset))
    for kind, values in class_anns:
        if kind == MIXIN:
            targets += [t.replace(".", "/") for t in values.get("value", []) +
                        values.get("targets", [])]
    if not targets:
        return None
    return MixinClass(name, targets, injections)


def _iter_classes(archive: zipfile.ZipFile, nested: str = "",
                  depth: int = 0) -> Iterator[Tuple[str, bytes]]:
    """(nested jar name or "", class bytes) for a jar and the jars nested in it."""
    for info in archive.infolist():
        n = info.filename
        if n.endswith(".class") and not n.startswith("META-INF/versions/"):
            yield nested, archive.read(info)
        elif depth < 3 and NESTED_JAR.match(n):
            try:
                with zipfile.ZipFile(io.BytesIO(archive.read(info))) as inner:
                    yield from _iter_classes(inner, n.rsplit("/", 1)[-1], depth + 1)
            except zipfile.BadZipFile:
                continue


def selector_name(selector: str) -> str:
    """'render(Lnet/minecraft/...;)V' / 'Lnet/minecraft/X;render(...)V' -> 'render'."""
    s = selector.split("(", 1)[0]
    if ";" in s:
        s = s.rsplit(";", 1)[-1]
    return s.split(":", 1)[0].strip() or selector


def jar_mixins(jar: "zipfile.ZipFile | bytes") -> Dict[str, Dict]:
    """{mixin class: {"nested": jar name or "", "targets": [...],
    "injections": [[kind, [method names]]]}} for every mixin in the jar."""
    archive = jar if isinstance(jar, zipfile.ZipFile) else zipfile.ZipFile(io.BytesIO(jar))
    found: Dict[str, Dict] = {}
    for nested, data in _iter_classes(archive):
        try:
            mixin = parse_mixin(data)
        except (ClassFileError, struct.error):
            continue
        if mixin and mixin.name not in found:
            found[mixin.name] = {
                "nested": nested, "targets": mixin.targets,
                "injections": [[i.kind, [selector_name(s) for s in i.methods]]
                               for i in mixin.injections]}
    return found


def aggregate(mixins: Dict[str, Dict]) -> Dict[str, Dict]:
    """{target class: {"mixins": n, "methods": {method name: [injector kinds]}}}."""
    profile: Dict[str, Dict] = {}
    for m in mixins.values():
        for target in m["targets"]:
            entry = profile.setdefault(target, {"mixins": 0, "methods": {}})
            entry["mixins"] += 1
            for kind, methods in m["injections"]:
                for name in methods:
                    entry["methods"].setdefault(name, []).append(kind)
    return profile


def jar_profile(jar: "zipfile.ZipFile | bytes") -> Dict[str, Dict]:
    return aggregate(jar_mixins(jar))
