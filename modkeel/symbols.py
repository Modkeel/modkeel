"""Minecraft symbol table and the checks that run against it.

See docs/symbol-check-design.md for the full design. In short: a mod's source names
Minecraft classes, Mixin targets and static members literally, so their existence can be
verified against official mappings without invoking a compiler.

This module owns the in-memory symbol table (``SymbolIndex``), the Java-to-JVM descriptor
conversion it needs, and the report produced by checking a scanned source tree against it.
Fetching and caching mappings lives in ``modkeel.mappings``; scanning source lives in
``modkeel.javascan``.
"""

import gzip
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger("modkeel")

INDEX_FORMAT_VERSION = 1

# Only these packages exist in the mappings. Everything else is a third-party API the
# symbol table knows nothing about, and must be ignored rather than reported missing.
CHECKED_PACKAGES = ("net.minecraft.", "com.mojang.")

PRIMITIVE_DESCRIPTORS = {
    "void": "V",
    "boolean": "Z",
    "byte": "B",
    "char": "C",
    "short": "S",
    "int": "I",
    "long": "J",
    "float": "F",
    "double": "D",
}


def is_candidate(fqcn: str) -> bool:
    """Coarse prefilter: a class name that *might* be described by the mappings.

    Not authoritative. ``com.mojang.brigadier`` and ``com.mojang.datafixers`` pass this
    filter but are separate unobfuscated libraries absent from the mappings entirely --
    use ``SymbolIndex.covers`` before concluding anything is missing.
    """
    return fqcn.startswith(CHECKED_PACKAGES)


def package_of(fqcn: str) -> str:
    return fqcn.rsplit(".", 1)[0] if "." in fqcn else ""


def to_descriptor(java_type: str) -> str:
    """Convert a Java type name to its JVM descriptor.

    ``int`` -> ``I``, ``boolean[]`` -> ``[Z``,
    ``net.minecraft.core.BlockPos`` -> ``Lnet/minecraft/core/BlockPos;``
    """
    java_type = java_type.strip()

    arrays = 0
    while java_type.endswith("[]"):
        arrays += 1
        java_type = java_type[:-2].strip()

    primitive = PRIMITIVE_DESCRIPTORS.get(java_type)
    base = primitive if primitive else f"L{java_type.replace('.', '/')};"

    return "[" * arrays + base


def method_descriptor(return_type: str, param_types: List[str]) -> str:
    """Build a JVM method descriptor from Java type names."""
    params = "".join(to_descriptor(p) for p in param_types if p.strip())
    return f"({params}){to_descriptor(return_type)}"


def split_params(params: str) -> List[str]:
    """Split a mappings parameter list. Generics never appear, so a comma split is safe."""
    params = params.strip()
    if not params:
        return []
    return [p.strip() for p in params.split(",") if p.strip()]


@dataclass
class SymbolIndex:
    """Every Minecraft class, field and method for one (flavor, version) pair."""

    flavor: str
    mc_version: str
    classes: Set[str] = field(default_factory=set)
    fields: Dict[str, Set[str]] = field(default_factory=dict)
    methods: Dict[str, Set[Tuple[str, int]]] = field(default_factory=dict)
    descriptors: Dict[str, Set[str]] = field(default_factory=dict)

    _packages: Optional[Set[str]] = field(default=None, repr=False, compare=False)

    # ── Lookups ──────────────────────────────────────────────────────────────

    @property
    def packages(self) -> Set[str]:
        """Every package containing at least one indexed class."""
        if self._packages is None:
            self._packages = {package_of(c) for c in self.classes}
        return self._packages

    def covers(self, fqcn: str) -> bool:
        """True when this index actually describes the class's package.

        Absence of a whole package means the mappings say nothing about it -- a bundled
        library such as ``com.mojang.brigadier``, or a third-party API. Classes there are
        unknown, never missing. Without this distinction, checking a real mod against real
        mappings reports its Brigadier and DataFixerUpper imports as breakage.
        """
        return package_of(fqcn) in self.packages

    def has_class(self, fqcn: str) -> bool:
        return fqcn in self.classes

    def has_field(self, fqcn: str, name: str) -> bool:
        return name in self.fields.get(fqcn, ())

    def has_method(self, fqcn: str, name: str, arity: Optional[int] = None) -> bool:
        entries = self.methods.get(fqcn, ())
        if arity is None:
            return any(n == name for n, _ in entries)
        return (name, arity) in entries

    def has_descriptor(self, fqcn: str, name: str, descriptor: str) -> bool:
        return f"{name}{descriptor}" in self.descriptors.get(fqcn, ())

    def has_member(self, fqcn: str, name: str) -> bool:
        """True when a name exists as either a field or a method on the class."""
        return self.has_field(fqcn, name) or self.has_method(fqcn, name)

    # ── Persistence ──────────────────────────────────────────────────────────

    def save(self, path: Path) -> None:
        """Serialize to gzipped JSON.

        JSON rather than pickle: the cache lives in the user's home directory and a
        deserializer that can execute code has no business reading from disk here.
        """
        payload = {
            "format": INDEX_FORMAT_VERSION,
            "flavor": self.flavor,
            "mc_version": self.mc_version,
            "classes": sorted(self.classes),
            "fields": {k: sorted(v) for k, v in self.fields.items()},
            "methods": {
                k: sorted(f"{n}/{a}" for n, a in v) for k, v in self.methods.items()
            },
            "descriptors": {k: sorted(v) for k, v in self.descriptors.items()},
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))

    @classmethod
    def load(cls, path: Path) -> Optional["SymbolIndex"]:
        """Read a cached index, or None when it is absent, corrupt or stale."""
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError, EOFError):
            return None

        if payload.get("format") != INDEX_FORMAT_VERSION:
            return None

        methods = {}
        for fqcn, entries in payload.get("methods", {}).items():
            parsed = set()
            for entry in entries:
                name, _, arity = entry.rpartition("/")
                if name and arity.isdigit():
                    parsed.add((name, int(arity)))
            methods[fqcn] = parsed

        return cls(
            flavor=payload["flavor"],
            mc_version=payload["mc_version"],
            classes=set(payload.get("classes", ())),
            fields={k: set(v) for k, v in payload.get("fields", {}).items()},
            methods=methods,
            descriptors={k: set(v) for k, v in payload.get("descriptors", {}).items()},
        )


# ── ProGuard mappings parsing ────────────────────────────────────────────────

CLASS_LINE = re.compile(r"^(\S+) -> (\S+):$")
# Members are indented. Methods carry parentheses and an optional line-number prefix.
METHOD_LINE = re.compile(r"^\s+(?:\d+:\d+:)?(\S+)\s+(\S+?)\((.*)\)\s+->\s+(\S+)$")
FIELD_LINE = re.compile(r"^\s+(\S+)\s+(\S+)\s+->\s+(\S+)$")


def parse_proguard_mappings(text: str, mc_version: str) -> SymbolIndex:
    """Parse Mojang's ProGuard-format mappings into a SymbolIndex.

    Only ``net.minecraft.*`` / ``com.mojang.*`` classes are indexed; the rest of the file
    describes types the checks are forbidden from reasoning about anyway.
    """
    index = SymbolIndex(flavor="mojmap", mc_version=mc_version)
    current: Optional[str] = None

    for line in text.splitlines():
        if not line or line.lstrip().startswith("#"):
            continue

        class_match = CLASS_LINE.match(line)
        if class_match:
            fqcn = class_match.group(1)
            current = fqcn if is_candidate(fqcn) else None
            if current:
                index.classes.add(current)
            continue

        if current is None:
            continue

        method_match = METHOD_LINE.match(line)
        if method_match:
            return_type, name, params, _obf = method_match.groups()
            if name == "<init>" or name == "<clinit>":
                continue
            param_types = split_params(params)
            index.methods.setdefault(current, set()).add((name, len(param_types)))
            index.descriptors.setdefault(current, set()).add(
                f"{name}{method_descriptor(return_type, param_types)}"
            )
            continue

        field_match = FIELD_LINE.match(line)
        if field_match:
            _type, name, _obf = field_match.groups()
            index.fields.setdefault(current, set()).add(name)

    return index


# ── Report ───────────────────────────────────────────────────────────────────

# Score penalties. See docs/symbol-check-design.md "Policy: evidence, not certainty".
PENALTY_MISSING_CLASS = -40
PENALTY_MISSING_MIXIN_TARGET = -60
PENALTY_MISSING_AT_DESCRIPTOR = -60
PENALTY_MISSING_MEMBER = -25
BONUS_CLEAN = 50

# Above this share of missing imports, the likely explanation is a misdetected mapping
# flavor rather than a genuinely broken mod. Reporting it as breakage would be the worst
# failure mode this layer has, so it disowns the result instead.
MISDETECTION_THRESHOLD = 0.5


@dataclass
class SymbolReport:
    """Outcome of checking a scanned source tree against a symbol table."""

    checked: bool = True
    skip_reason: Optional[str] = None
    mc_version: Optional[str] = None
    flavor: Optional[str] = None

    missing_classes: List[str] = field(default_factory=list)
    missing_mixin_targets: List[str] = field(default_factory=list)
    missing_at_targets: List[str] = field(default_factory=list)
    missing_members: List[str] = field(default_factory=list)

    classes_checked: int = 0
    mixins_checked: int = 0

    @classmethod
    def skipped(cls, reason: str) -> "SymbolReport":
        return cls(checked=False, skip_reason=reason)

    @property
    def is_clean(self) -> bool:
        return self.checked and not self.findings

    @property
    def findings(self) -> List[str]:
        return (
            [f"class not in {self.mc_version}: {c}" for c in self.missing_classes]
            + [f"mixin target missing: {t}" for t in self.missing_mixin_targets]
            + [f"@At target missing: {t}" for t in self.missing_at_targets]
            + [f"member missing: {m}" for m in self.missing_members]
        )

    @property
    def score_delta(self) -> int:
        if not self.checked:
            return 0
        if self.is_clean:
            return BONUS_CLEAN
        return (
            len(self.missing_classes) * PENALTY_MISSING_CLASS
            + len(self.missing_mixin_targets) * PENALTY_MISSING_MIXIN_TARGET
            + len(self.missing_at_targets) * PENALTY_MISSING_AT_DESCRIPTOR
            + len(self.missing_members) * PENALTY_MISSING_MEMBER
        )

    @property
    def summary(self) -> str:
        if not self.checked:
            return f"symbol check skipped ({self.skip_reason})"
        if self.is_clean:
            return (
                f"{self.classes_checked} classes and {self.mixins_checked} mixin "
                f"targets resolve against {self.mc_version}"
            )
        counts = []
        if self.missing_classes:
            counts.append(f"{len(self.missing_classes)} missing classes")
        if self.missing_mixin_targets:
            counts.append(f"{len(self.missing_mixin_targets)} missing mixin targets")
        if self.missing_at_targets:
            counts.append(f"{len(self.missing_at_targets)} missing @At targets")
        if self.missing_members:
            counts.append(f"{len(self.missing_members)} missing members")
        return ", ".join(counts)


def check_references(refs, index: SymbolIndex) -> SymbolReport:
    """Check scanned source references against a symbol table.

    ``refs`` is a ``modkeel.javascan.SourceRefs``. Passed structurally rather than
    imported so this module stays independent of the scanner.
    """
    report = SymbolReport(mc_version=index.mc_version, flavor=index.flavor)

    if refs.uses_preprocessor:
        return SymbolReport.skipped("source uses a version preprocessor")
    if refs.multi_version_sources:
        return SymbolReport.skipped("repository has multi-version source sets")

    # Coarse prefilter first, then the index's own package coverage. Anything in a package
    # the mappings do not describe is unknown, not missing.
    candidates = sorted(c for c in refs.imports if is_candidate(c))
    checkable_imports = [c for c in candidates if index.covers(c)]
    report.classes_checked = len(checkable_imports)

    if not checkable_imports:
        return SymbolReport.skipped("no checkable Minecraft imports found")

    missing = [
        c
        for c in checkable_imports
        if not index.has_class(c) and c not in refs.declared_classes
    ]

    # Wholesale absence means we are reading the wrong mapping set, not that the mod
    # references hundreds of nonexistent classes.
    if len(missing) / len(checkable_imports) > MISDETECTION_THRESHOLD:
        return SymbolReport.skipped(
            f"{len(missing)}/{len(checkable_imports)} imports missing "
            f"-- assuming wrong mapping flavor, not breakage"
        )

    report.missing_classes = missing

    for target in sorted(refs.mixin_targets):
        fqcn = refs.resolve(target)
        if not fqcn or not is_candidate(fqcn) or not index.covers(fqcn):
            continue
        if fqcn in refs.declared_classes:
            continue
        report.mixins_checked += 1
        if not index.has_class(fqcn):
            report.missing_mixin_targets.append(fqcn)

    # Only the owner class is checked, never the member. Mixin resolves @At targets
    # through the class hierarchy at runtime, so pointing at a method a supertype
    # declares is both legal and common -- and ProGuard mappings carry no hierarchy.
    # Measured against Create mc1.21.1/dev: descriptor checking produced 12 findings,
    # all inherited (LivingEntity.isInLava from Entity, Registry.forEach from Iterable)
    # or added by NeoForge patches to vanilla classes.
    for owner, name, _descriptor in sorted(refs.at_targets):
        if not is_candidate(owner) or not index.covers(owner):
            continue
        if owner in refs.declared_classes:
            continue
        report.mixins_checked += 1
        if not index.has_class(owner):
            report.missing_at_targets.append(f"{owner}.{name}")

    for fqcn, member in sorted(refs.static_refs):
        if not is_candidate(fqcn) or not index.has_class(fqcn):
            continue
        if not index.has_member(fqcn, member):
            report.missing_members.append(f"{fqcn}.{member}")

    return report
