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

``check_mixin_targets`` uses the same reading to ask whether a build's mixins still apply on
another Minecraft version (see its docstring): a mixin that fails to apply stops the game at
startup, and neither the metadata nor the class/member linkage check can see it.
"""

import io
import json
import re
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from modkeel.linkage import ClassFileError, _class_internal_name, _parse_pool, _utf8

MIXIN = "Lorg/spongepowered/asm/mixin/Mixin;"
OVERWRITE = "Lorg/spongepowered/asm/mixin/Overwrite;"
INJECTOR_PACKAGES = ("Lorg/spongepowered/asm/mixin/injection/",
                     "Lcom/llamalad7/mixinextras/injector/")
NESTED_JAR = re.compile(r"META-INF/(jars|jarjar)/.+\.jar$")
# injectors that replace behaviour rather than add to it
REPLACING = {"Overwrite", "Redirect", "WrapOperation", "WrapWithCondition", "ModifyConstant"}
CALLBACK_INFO = ("Lorg/spongepowered/asm/mixin/injection/callback/CallbackInfo;",
                 "Lorg/spongepowered/asm/mixin/injection/callback/CallbackInfoReturnable;")


@dataclass
class Injection:
    kind: str           # simple annotation name: Inject, Redirect, Overwrite, ...
    methods: List[str]  # target method selectors as written ("render", "tick()V", ...)
    handler: str = ""   # the mixin method carrying the annotation: name + descriptor
    require: Optional[int] = None  # the annotation's require = N, when written
    # its @At injection points as (value, target): ("INVOKE", "Lnet/x/A;m(I)V"), ("HEAD", "")
    points: List[Tuple[str, str]] = field(default_factory=list)


@dataclass
class MixinClass:
    name: str
    targets: List[str]
    injections: List[Injection] = field(default_factory=list)


def _annotations(attr: bytes, pool) -> List[Tuple[str, Dict[str, list]]]:
    """Every top-level annotation as (type descriptor, {element: values}).

    Values are strings (string, class and int constants) or, for a nested annotation such
    as an injector's @At, that annotation's own {element: values} dict.
    """
    out: List[Tuple[str, Dict[str, List[str]]]] = []

    def u2(at: int) -> int:
        return struct.unpack_from(">H", attr, at)[0]

    def element(at: int, sink: Optional[List[str]]) -> int:
        tag = chr(attr[at])
        at += 1
        if tag in "BCDFIJSZs":
            if sink is not None and tag == "s":
                sink.append(_utf8(pool, u2(at)) or "")
            elif sink is not None and tag == "I":  # int constants, e.g. require = 1
                entry = pool[u2(at)]
                if entry and entry[0] == 3:
                    sink.append(str(struct.unpack(">i", entry[1])[0]))
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
            if sink is None:
                return annotation(at, None)
            nested: list = []
            at = annotation(at, nested)
            sink.append(nested[0][1])
            return at
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


def _first_str(values: Optional[list]) -> str:
    return next((v for v in values or () if isinstance(v, str)), "")


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
        handler = method_name + (_utf8(pool, u2(offset + 4)) or "")
        anns, offset = annotated(offset + 8, u2(offset + 6))
        for kind, values in anns:
            require = values.get("require")
            require = int(require[0]) if require and require[0].lstrip("-").isdigit() else None
            if kind == OVERWRITE:
                injections.append(Injection("Overwrite", [handler], handler, require))
            elif kind.startswith(INJECTOR_PACKAGES) and "method" in values:
                points = [(_first_str(a.get("value")), _first_str(a.get("target")))
                          for a in values.get("at", []) if isinstance(a, dict)]
                injections.append(Injection(_simple(kind), values["method"], handler, require,
                                            points))
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


# ---------------------------------------------------------------------------
# Do this build's mixins still apply on another Minecraft version?
# ---------------------------------------------------------------------------

@dataclass
class MixinReport:
    """What check_mixin_targets found.

    fatal: injections Mixin refuses at startup (the game does not start). warnings: targets
    that vanished under an injector that may be skipped (require 0): the game starts, that
    feature silently does nothing. checked: injections that could be judged at all.
    """

    checked: int = 0
    fatal: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _params(desc: str) -> List[str]:
    """Parameter descriptors of a method descriptor: "(ILa/B;[J)V" -> ["I", "La/B;", "[J"]."""
    body, out, i = desc[1:desc.index(")")], [], 0
    while i < len(body):
        start = i
        while body[i] == "[":
            i += 1
        i = body.index(";", i) + 1 if body[i] == "L" else i + 1
        out.append(body[start:i])
    return out


def _split_selector(selector: str) -> Tuple[str, Optional[str]]:
    """'render' -> ('render', None); 'Lnet/X;tick(J)V' -> ('tick', '(J)V')."""
    s = selector.strip()
    if ";" in s.split("(", 1)[0]:
        s = s.split(";", 1)[1]
    name, paren, rest = s.partition("(")
    return name.split(":", 1)[0].strip(), (paren + rest) if paren else None


def _descriptors(index, owner: str, name: str) -> List[str]:
    """Descriptors of every method `name` the class declares in that symbol table."""
    prefix = name + "("
    return [d[len(name):] for d in index.descriptors.get(owner, ()) if d.startswith(prefix)]


def _default_requires(archive: zipfile.ZipFile) -> Dict[str, int]:
    """{mixin package path: injectors.defaultRequire} from the jar's mixin configs."""
    out: Dict[str, int] = {}
    for info in archive.infolist():
        # Configs are small JSON files, usually at the root; the content says which are
        if not info.filename.endswith(".json") or info.file_size > 256 * 1024:
            continue
        try:
            config = json.loads(archive.read(info))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(config, dict) and isinstance(config.get("package"), str) and any(
                isinstance(config.get(k), list) for k in ("mixins", "client", "server")):
            injectors = config.get("injectors") or {}
            require = injectors.get("defaultRequire", 0) if isinstance(injectors, dict) else 0
            out[config["package"].replace(".", "/") + "/"] = int(require or 0)
    return out


INTERMEDIARY_TARGET = re.compile(r"^net/minecraft/class_\d+$")


def uses_intermediary(jar_path: Path) -> bool:
    """Whether the JAR's mixins target the game by intermediary names (Fabric JARs for
    obfuscated versions): their @Mixin class literals are remapped at build time."""
    with zipfile.ZipFile(jar_path) as archive:
        for info in archive.infolist():
            if not info.filename.endswith(".class") or \
                    info.filename.startswith("META-INF/versions/"):
                continue
            try:
                mixin = parse_mixin(archive.read(info))
            except (ClassFileError, struct.error):
                continue
            if mixin and any(INTERMEDIARY_TARGET.match(t) for t in mixin.targets):
                return True
    return False


def _refmaps(archive: zipfile.ZipFile) -> Dict[str, Dict[str, str]]:
    """{mixin class: {selector as written: selector in the JAR's names}} from refmaps.

    A JAR remapped at build (Fabric to intermediary) keeps its mixins' strings (method
    selectors, @At targets, string @Mixin targets) in the names the author wrote and ships
    a refmap the mixin config points to, which Mixin reads at runtime.
    """
    out: Dict[str, Dict[str, str]] = {}
    names = set(archive.namelist())
    for info in archive.infolist():
        if not info.filename.endswith(".json") or info.file_size > 256 * 1024:
            continue
        try:
            config = json.loads(archive.read(info))
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(config, dict) or not isinstance(config.get("refmap"), str) \
                or config["refmap"] not in names:
            continue
        try:
            refmap = json.loads(archive.read(config["refmap"]))
        except (ValueError, UnicodeDecodeError, KeyError):
            continue
        if not isinstance(refmap, dict):
            continue
        tables = [refmap.get("mappings")]
        tables += list((refmap.get("data") or {}).values()) \
            if isinstance(refmap.get("data"), dict) else []
        for table in tables:
            if not isinstance(table, dict):
                continue
            for mixin, entries in table.items():
                if isinstance(entries, dict):
                    out.setdefault(mixin.replace(".", "/"), {}).update(
                        {k: v for k, v in entries.items() if isinstance(v, str)})
    return out


def _remapped(mixin: MixinClass, remap: Dict[str, str]) -> MixinClass:
    """The mixin with its strings translated through its refmap entries."""
    if not remap:
        return mixin
    targets = [remap.get(t, remap.get(t.replace("/", "."), t)).replace(".", "/")
               for t in mixin.targets]
    injections = [Injection(i.kind, [remap.get(m, m) for m in i.methods], i.handler,
                            i.require, [(v, remap.get(t, t)) for v, t in i.points])
                  for i in mixin.injections]
    return MixinClass(mixin.name, targets, injections)


def check_mixin_targets(jar_path: Path, index, built_for_index) -> MixinReport:
    """Which of the JAR's mixins would fail to apply on `index`'s version.

    Built for `built_for_index`'s version, a mixin names target methods by name (and
    sometimes descriptor). Judged only where the built-for symbol table shows the injection
    resolved there, so mappings the JAR is not in (intermediary, SRG) and targets the game
    does not declare (loader patches, other mods) are skipped, never reported:

    - a target method the class declared in built_for and no longer declares: the
      injection finds nothing. Fatal when the injector requires a match (its require, or
      its config's injectors.defaultRequire, >= 1), a warning otherwise. An @Overwrite of
      a vanished method is fatal.
    - an @Inject whose handler takes the target's parameters (before CallbackInfo): Mixin
      validates them against the target found. If built_for's method of that name had
      those parameters and every method of that name in the target has others, Mixin
      rejects the handler ("Invalid descriptor") and the game stops. Monsters in the Closet
      1.0.3 (built for 1.21.10) on 1.21.11: lambda$useWithoutItem$2 went from
      (Player, Player$BedSleepingProblem) to (Player, Component).
    - an injector whose method still exists but whose every @At(INVOKE/FIELD) point names a
      call or field its owner declared in built_for and no longer declares: nothing in the
      method matches. Fatal or a warning by the same require rule (_vanished_points).

    Mixins of nested jars (bundled libraries) are left out: their own mods carry them.
    A JAR remapped at build (Fabric, intermediary) is read through its refmap: pass
    indexes in the JAR's names (mappings.load_index(v, "intermediary")).
    """
    report = MixinReport()
    with zipfile.ZipFile(jar_path) as archive:
        requires = _default_requires(archive)
        refmaps = _refmaps(archive)
        for info in archive.infolist():
            if not info.filename.endswith(".class") or \
                    info.filename.startswith("META-INF/versions/"):
                continue
            data = archive.read(info)
            try:
                mixin = parse_mixin(data)
            except (ClassFileError, struct.error):
                continue
            if mixin is None:
                continue
            mixin = _remapped(mixin, refmaps.get(mixin.name, {}))
            default = next((r for pkg, r in requires.items()
                            if mixin.name.startswith(pkg)), 0)
            for target in mixin.targets:
                owner = target.replace("/", ".")
                if owner not in built_for_index.classes or owner not in index.classes:
                    continue  # unreadable names, or a class linkage already reports
                for injection in mixin.injections:
                    _judge(report, mixin.name, owner, injection, default, index,
                           built_for_index)
    return report


def _judge(report: MixinReport, mixin: str, owner: str, injection: Injection,
           default_require: int, index, built_for_index) -> None:
    short = f"{mixin.rsplit('/', 1)[-1]} -> {owner.rsplit('.', 1)[-1]}"
    found_any, vanished = False, []
    for selector in injection.methods:
        name, desc = _split_selector(selector)
        if not name or "*" in name or name.startswith("/"):
            continue  # wildcard or /regex/ selector: cannot be judged by name ($ is legal:
            # lambda$useWithoutItem$2)
        before = _descriptors(built_for_index, owner, name)
        after = _descriptors(index, owner, name)
        if desc:
            before = [d for d in before if d == desc]
            after = [d for d in after if d == desc]
        if after and not before:
            found_any = True  # a selector written for this version (multi-version mods)
            continue
        if not before:
            continue  # it did not resolve against the game there either
        report.checked += 1
        if not after:
            vanished.append(name)
            continue
        found_any = True
        if injection.kind == "Inject" and injection.handler:
            handler_params = _params(injection.handler[injection.handler.index("("):])
            cut = next((i for i, p in enumerate(handler_params) if p in CALLBACK_INFO), None)
            wanted = handler_params[:cut] if cut else []
            if wanted and any(_params(d) == wanted for d in before) and \
                    not any(_params(d) == wanted for d in after):
                report.fatal.append(
                    f"{short}.{name}: parameters changed, @Inject handler no longer matches")
    require = injection.require if injection.require is not None else default_require
    if vanished and not found_any:
        line = f"{short}.{', '.join(vanished)}: target method gone ({injection.kind})"
        if injection.kind == "Overwrite" or require >= 1:
            report.fatal.append(line)
        else:
            report.warnings.append(line)
    elif found_any and injection.points:
        gone = _vanished_points(injection.points, index, built_for_index)
        if gone:
            report.checked += 1
            line = (f"{short}.{', '.join(_split_selector(m)[0] for m in injection.methods)}: "
                    f"injection point gone ({injection.kind} at {', '.join(gone)})")
            (report.fatal if require >= 1 else report.warnings).append(line)


def _split_member(target: str) -> Tuple[str, str]:
    """An @At target as (owner fqcn, member): Mixin accepts "Lnet/x/A;m(I)V", "net/x/A;m"
    and "net/x/A.m(I)V" alike. ("", "") when it names no owner."""
    head = target.split("(", 1)[0].split(":", 1)[0]
    if ";" in head:
        owner, _, member = target.partition(";")
        owner = owner[1:] if owner.startswith("L") else owner
    elif "." in head:
        owner, member = head.rsplit(".", 1)[0], target[len(head.rsplit(".", 1)[0]) + 1:]
    else:
        return "", ""
    return owner.replace("/", "."), member


# @At values whose target names a call or a field access
INVOKE_POINTS = ("INVOKE", "INVOKE_ASSIGN", "INVOKE_STRING")


def _vanished_points(points: List[Tuple[str, str]], index, built_for_index) -> List[str]:
    """The injection's call/field points that vanished, when every one of them did.

    An injector applies where any of its points matches, so one surviving point (or one
    that cannot be judged: HEAD, RETURN, a call into another mod, a member the owner only
    inherits) means it may still apply, and nothing is reported. A point is gone when its
    owner declared that method (with that descriptor) or field in built_for and no longer
    declares it: the call cannot be in the target method any more, so nothing matches.
    """
    gone = []
    for value, target in points:
        if value not in INVOKE_POINTS and value != "FIELD":
            return []
        owner, member = _split_member(target)
        if not owner:
            return []
        if owner not in built_for_index.classes or owner not in index.classes:
            return []
        short = f"{owner.rsplit('.', 1)[-1]}.{member.split('(', 1)[0].split(':', 1)[0]}"
        if value == "FIELD":
            name = member.split(":", 1)[0]
            if name not in built_for_index.fields.get(owner, ()) or \
                    name in index.fields.get(owner, ()):
                return []
            gone.append(short)
            continue
        name, desc = _split_selector(member)
        before = _descriptors(built_for_index, owner, name)
        after = _descriptors(index, owner, name)
        if desc:
            before, after = [d for d in before if d == desc], [d for d in after if d == desc]
        if not before or after:
            return []
        gone.append(short + "()")
    return gone
