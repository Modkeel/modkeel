"""Scan Java source for the Minecraft references that can be checked without a compiler.

Tier A of docs/symbol-check-design.md: imports and Mixin annotations. Both name their
targets as literals in predictable syntactic positions, so regex is reliable here rather
than merely expedient. Tier B (tree-sitter, for static member references and overrides)
is not implemented yet.

Nothing in this module resolves types. If a reference requires knowing the type of an
expression, it is out of scope by design.
"""

import io
import logging
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import requests

logger = logging.getLogger("modforge")

IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;", re.MULTILINE)

# @Mixin(Foo.class) / @Mixin({A.class, B.class}) / @Mixin(value = Foo.class, priority = 1)
MIXIN_CLASS_RE = re.compile(r"@Mixin\s*\(([^)]*)\)", re.DOTALL)
CLASS_LITERAL_RE = re.compile(r"([\w.]+)\s*\.\s*class")
# @Mixin(targets = "net.minecraft.foo.Bar") -- also accepts JVM-internal form.
TARGETS_RE = re.compile(r"targets\s*=\s*\{?\s*\"([^\"]+)\"")

# @At(target = "Lnet/minecraft/world/level/Level;getBlockState(Lnet/...;)L...;")
AT_TARGET_RE = re.compile(r"target\s*=\s*\"L([\w/$]+);([\w$<>]+)(\([^\"]*)\"")
# @At(target = "Lnet/minecraft/world/level/Level;field:Ltype;") -- field form, no parens.
AT_FIELD_RE = re.compile(r"target\s*=\s*\"L([\w/$]+);([\w$]+):L?([\w/$;\[]+)\"")

# Preprocessor directives (ReplayMod/Preprocessor, Manifold) leave inactive code in place
# that legitimately references symbols absent from the target version.
PREPROCESSOR_RE = re.compile(r"^\s*//#(if|else|elseif|endif|ifdef)", re.MULTILINE)

SOURCE_ROOT_RE = re.compile(r"src/main/java(\d+)?/")


@dataclass
class SourceRefs:
    """Minecraft references extracted from a source tree."""

    imports: Set[str] = field(default_factory=set)
    mixin_targets: Set[str] = field(default_factory=set)
    # (ownerFqcn, memberName, descriptor) -- descriptor is "" for field targets.
    at_targets: Set[Tuple[str, str, str]] = field(default_factory=set)
    static_refs: Set[Tuple[str, str]] = field(default_factory=set)

    uses_preprocessor: bool = False
    multi_version_sources: bool = False
    files_scanned: int = 0

    def resolve(self, name: str) -> Optional[str]:
        """Resolve a simple class name to an FQCN using the imports seen.

        Already-qualified names pass through. Unresolvable simple names return None,
        which callers must treat as "unknown", never as "missing".
        """
        if "." in name and not name[0].isupper():
            return name
        for fqcn in self.imports:
            if fqcn.rsplit(".", 1)[-1] == name:
                return fqcn
        return None

    def merge(self, other: "SourceRefs") -> None:
        self.imports |= other.imports
        self.mixin_targets |= other.mixin_targets
        self.at_targets |= other.at_targets
        self.static_refs |= other.static_refs
        self.uses_preprocessor |= other.uses_preprocessor
        self.multi_version_sources |= other.multi_version_sources
        self.files_scanned += other.files_scanned


def scan_source(text: str) -> SourceRefs:
    """Extract references from a single Java source file."""
    refs = SourceRefs(files_scanned=1)

    refs.imports = set(IMPORT_RE.findall(text))
    refs.uses_preprocessor = bool(PREPROCESSOR_RE.search(text))

    for block in MIXIN_CLASS_RE.findall(text):
        refs.mixin_targets |= set(CLASS_LITERAL_RE.findall(block))
        for target in TARGETS_RE.findall(block):
            refs.mixin_targets.add(target.replace("/", "."))

    for owner, name, descriptor in AT_TARGET_RE.findall(text):
        refs.at_targets.add((owner.replace("/", "."), name, descriptor))

    for owner, name, _type in AT_FIELD_RE.findall(text):
        refs.at_targets.add((owner.replace("/", "."), name, ""))

    return refs


def scan_tree(root: Path, max_files: int = 4000) -> SourceRefs:
    """Scan every Java file under a directory."""
    merged = SourceRefs()
    source_roots: Set[str] = set()

    for path in sorted(root.rglob("*.java")):
        if merged.files_scanned >= max_files:
            logger.debug("scan_tree hit max_files=%d, stopping", max_files)
            break

        posix = path.as_posix()
        match = SOURCE_ROOT_RE.search(posix)
        if match:
            source_roots.add(match.group(0))

        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        merged.merge(scan_source(text))

    # src/main/java, src/main/java17, src/main/java21 side by side means version-specific
    # sources; symbols absent from one variant are expected, not broken.
    merged.multi_version_sources = len(source_roots) > 1

    return merged


def fetch_source_tree(
    owner: str, repo: str, ref: str, dest: Path, timeout: int = 120
) -> Optional[Path]:
    """Download and unpack a repository tarball. Returns the extracted root, or None.

    One HTTP GET, no git binary, discarded by the caller afterwards.
    """
    url = f"https://codeload.github.com/{owner}/{repo}/tar.gz/{ref}"

    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as e:
        logger.debug("tarball fetch failed for %s/%s@%s: %s", owner, repo, ref, e)
        return None

    dest.mkdir(parents=True, exist_ok=True)

    try:
        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
            members = [m for m in archive.getmembers() if _is_safe_member(m, dest)]
            archive.extractall(dest, members=members)
    except (tarfile.TarError, OSError) as e:
        logger.debug("tarball extract failed for %s/%s@%s: %s", owner, repo, ref, e)
        return None

    roots = [p for p in dest.iterdir() if p.is_dir()]
    return roots[0] if len(roots) == 1 else dest


def _is_safe_member(member: tarfile.TarInfo, dest: Path) -> bool:
    """Reject archive entries that would escape the destination directory."""
    if member.issym() or member.islnk():
        return False
    resolved = (dest / member.name).resolve()
    try:
        resolved.relative_to(dest.resolve())
    except ValueError:
        return False
    return True
