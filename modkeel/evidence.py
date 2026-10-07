"""Evidence layer of the resolver: ordered checks that say how sure we are a JAR runs.

Resolution has three layers (modkeel/resolve.py): the target, the sources that produce a
JAR, and this one. Every check has the same shape, takes a Subject (the JAR, the target,
what it was built for, its dependencies) and returns an Outcome:

  passed    the check ran and the JAR passed it
  failed    the check ran and the JAR will not work (the reason says why)
  not_run   the check could not say (no Docker, no symbol table, unreadable names,
            client-only mod on a server): never a pass, and the reason says why

Checks run cheapest first, in EVIDENCE_ORDER:

  metadata       ms        the loader would accept it: declared loader and Minecraft range
  linkage        seconds   every Minecraft class it uses exists on the target and, when the
                           version it was built for is known, no method or field it calls
                           was removed or renamed since (modkeel/linkage.py)
  mixins         seconds   its mixins still apply: every target method they name still
                           exists and every @Inject handler still matches its parameters,
                           when the version it was built for is known (modkeel/mixinscan.py)
  docker_server  ~1 min    a headless server boots with it and its dependencies
  client         minutes   a real client boots (the Companion e2e; not wired here yet)

`gather` runs the checks a caller asks for and stops at the first failure, or at the first
*required* check that could not run: a strategy that needs a server boot (relaxed builds)
treats "no Docker" as a rejection, while one that only offers the boot as extra evidence
(`get --docker-test`) carries on without it. A strategy decides what it requires; this
module decides how each check is done, so a better check (or a new one) lands everywhere.

Pack-level tests (all of a compile run's JARs in one server, then each alone) stay in
DockerTester.test_mods_in_docker: they test a set, not one JAR.
"""

import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from modkeel.build import validate_jar
from modkeel.models import ModCompilerConfig

EVIDENCE_ORDER = ["metadata", "linkage", "mixins", "docker_server", "client"]

PASSED, FAILED, NOT_RUN = "passed", "failed", "not_run"

# How each kind of evidence is named to the user. Besides the checks above, sources record
# what they know without checking: "published" (the author's build for this exact target),
# "built" (compiled from source for it), "metadata_relaxed" (Modkeel widened the range).
EVIDENCE_LABELS = {
    "published": "official build for this version",
    "built": "compiled for this version",
    "metadata": "metadata",
    "metadata_relaxed": "range widened by Modkeel",
    "linkage": "linkage",
    "mixins": "mixins",
    "docker_server": "server boot",
    "client": "client",
}


@dataclass
class Subject:
    """The JAR under test and what it should run on."""

    jar: Path
    mc_version: str
    built_for: Optional[str] = None       # the version the build targets, when known
    name: str = ""
    dependencies: List[Path] = field(default_factory=list)


@dataclass
class Outcome:
    """One check's result. `data` carries the check's own report for callers that print it
    (the LinkageReport for linkage)."""

    check: str
    status: str
    detail: str = ""
    data: Any = None

    @property
    def passed(self) -> bool:
        return self.status == PASSED


@dataclass
class Evidence:
    """What `gather` found: one Outcome per check that ran, in order."""

    outcomes: List[Outcome] = field(default_factory=list)
    required: frozenset = frozenset()

    @property
    def passed(self) -> List[str]:
        return [o.check for o in self.outcomes if o.passed]

    @property
    def failure(self) -> Optional[Outcome]:
        """The outcome that stopped it: a failed check, or a required one that could not
        run. None when every required check passed."""
        return next((o for o in self.outcomes if o.status == FAILED
                     or (o.status == NOT_RUN and o.check in self.required)), None)

    @property
    def ok(self) -> bool:
        return self.failure is None

    @property
    def reason(self) -> str:
        failure = self.failure
        return failure.detail if failure else ""


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

def check_metadata(subject: Subject, config: ModCompilerConfig) -> Outcome:
    """Would the loader accept it on the target: declared loader and Minecraft range."""
    # Size is a build-output heuristic, not metadata: a published file is what it is
    ok, _, _, message = validate_jar(subject.jar, subject.mc_version, min_size=0)
    if ok:
        return Outcome("metadata", PASSED)
    return Outcome("metadata", FAILED,
                   message.replace("JAR declares incompatible MC version",
                                   "its metadata only allows MC"))


def check_linkage(subject: Subject, config: ModCompilerConfig) -> Outcome:
    """Does its bytecode resolve on the target: classes, and members when built_for is known.

    Members compare two symbol tables (linkage.find_vanished_members); if the built-for
    mappings cannot be loaded, only classes are checked. The rejection names the first
    classes and calls, which say what part of the mod breaks and how big a port would be.
    """
    from modkeel.linkage import check_jar
    from modkeel.mappings import load_index

    target, built_for = subject.mc_version, subject.built_for
    index = load_index(target)
    if index is None:
        return Outcome("linkage", NOT_RUN, f"cannot verify: no symbol table for MC {target}")
    built_for_index = load_index(built_for) if built_for and built_for != target else None
    report = check_jar(subject.jar, index, built_for_index)
    if not report.checked:
        return Outcome("linkage", NOT_RUN, f"cannot verify ({report.skip_reason})", report)
    if report.is_clean:
        return Outcome("linkage", PASSED, report.summary, report)
    reasons = []
    if report.missing_classes:
        reasons.append(f"{len(report.missing_classes)} Minecraft classes it uses don't "
                       f"exist in {target} ({_first(report.missing_classes, _short_class)})")
    if report.vanished_members:
        reasons.append(f"{len(report.vanished_members)} methods/fields it calls were "
                       f"removed or renamed after {built_for} "
                       f"({_first(report.vanished_members, _short_member)})")
    return Outcome("linkage", FAILED, "; ".join(reasons), report)


def check_mixins(subject: Subject, config: ModCompilerConfig) -> Outcome:
    """Do its mixins still apply on the target (mixinscan.check_mixin_targets)?

    Needs the version the build targets: a mixin is judged by comparing what its targets
    were there with what they are on the target. Without it, or without either symbol
    table, not_run. Injections that may be skipped (require 0) only add a note: the game
    starts, that feature does nothing.
    """
    from modkeel.mappings import load_index
    from modkeel.mixinscan import check_mixin_targets

    target, built_for = subject.mc_version, subject.built_for
    if not built_for or built_for == target:
        return Outcome("mixins", NOT_RUN, "needs the version the build targets")
    index, built_for_index = load_index(target), load_index(built_for)
    if index is None or built_for_index is None:
        return Outcome("mixins", NOT_RUN, f"no symbol table for MC {target} or {built_for}")
    try:
        report = check_mixin_targets(subject.jar, index, built_for_index)
    except (OSError, zipfile.BadZipFile) as e:
        return Outcome("mixins", NOT_RUN, f"unreadable JAR ({e})")
    if report.fatal:
        more = f" (+{len(report.fatal) - 2} more)" if len(report.fatal) > 2 else ""
        return Outcome("mixins", FAILED,
                       f"{len(report.fatal)} mixin injections would fail to apply on "
                       f"{target}: {'; '.join(report.fatal[:2])}{more}", report)
    note = (f"; {len(report.warnings)} optional ones lost their target"
            if report.warnings else "")
    return Outcome("mixins", PASSED,
                   f"{report.checked} mixin injections still apply{note}", report)


def check_server_boot(subject: Subject, config: ModCompilerConfig) -> Outcome:
    """Does a headless server boot with it and its dependencies (DockerTester and its cache).

    No Docker, an infrastructure error and a client-only mod (a server cannot load it) all
    say nothing about whether it runs: not_run, with the reason.
    """
    from modkeel.docker import DockerTester
    from modkeel.models import CompilationResult

    tester = DockerTester(config)
    if not tester.check_docker_available():
        return Outcome("docker_server", NOT_RUN, "needs Docker (a server must boot with it)")
    results = [CompilationResult(repo_url=str(subject.jar), success=True,
                                 jar_path=str(subject.jar), mod_name=subject.name)]
    results += [CompilationResult(repo_url=str(d), success=True, jar_path=str(d),
                                  modrinth_download=True) for d in subject.dependencies]
    tester.test_mods_in_docker(results)
    main = results[0]
    if main.docker_test_passed:
        return Outcome("docker_server", PASSED, f"a MC {subject.mc_version} server booted")
    if main.docker_test_passed is None:
        return Outcome("docker_server", NOT_RUN,
                       f"server test inconclusive ({main.docker_error or 'no result'})")
    return Outcome("docker_server", FAILED,
                   f"a server did not boot with it ({(main.docker_error or '')[:160]})")


# Looked up at call time, so a check can be replaced (tests patch the functions above).
CHECKS: Dict[str, Callable[[Subject, ModCompilerConfig], Outcome]] = {
    "metadata": lambda s, c: check_metadata(s, c),
    "linkage": lambda s, c: check_linkage(s, c),
    "mixins": lambda s, c: check_mixins(s, c),
    "docker_server": lambda s, c: check_server_boot(s, c),
}


def gather(subject: Subject, config: ModCompilerConfig, checks: Iterable[str],
           required: Optional[Iterable[str]] = None) -> Evidence:
    """Run `checks` cheapest first; stop at a failure or a required check that cannot run.

    required defaults to every check asked for. A check name without an implementation
    (client, for now) is recorded as not_run.
    """
    checks = sorted(set(checks), key=EVIDENCE_ORDER.index)
    evidence = Evidence(required=frozenset(checks if required is None else required))
    for name in checks:
        run = CHECKS.get(name)
        outcome = (run(subject, config) if run
                   else Outcome(name, NOT_RUN, f"no {name} check available"))
        evidence.outcomes.append(outcome)
        if evidence.failure is not None:
            break
    return evidence


def evidence_line(passed: Iterable[str], docker_requested: bool = False) -> str:
    """One line for the user: what this JAR passed, and the stronger checks not run.

    "linkage ✓ · metadata ✓ · server boot not run (--docker-test)"
    """
    passed = list(passed)
    parts = [f"{EVIDENCE_LABELS.get(p, p)} ✓" for p in passed]
    if "docker_server" not in passed and not docker_requested:
        parts.append("server boot not run (--docker-test)")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Short names for rejections
# ---------------------------------------------------------------------------

def _first(items: List[str], short: Callable[[str], str], shown: int = 3) -> str:
    """The first `shown` items in short form, then "..." if there are more."""
    more = ", ..." if len(items) > shown else ""
    return ", ".join(short(i) for i in items[:shown]) + more


def _short_class(fqcn: str) -> str:
    """net.minecraft.client.renderer.DimensionSpecialEffects -> DimensionSpecialEffects."""
    return fqcn.rsplit(".", 1)[-1].rsplit("/", 1)[-1]


def _short_member(entry: str) -> str:
    """"method net.minecraft.x.Owner$Inner.name(I)V" -> "Owner$Inner.name()"; fields alike."""
    kind, _, ref = entry.partition(" ")
    if kind == "method":
        ref = ref.split("(", 1)[0]
    owner, _, name = ref.rpartition(".")
    return f"{_short_class(owner)}.{name}" + ("()" if kind == "method" else "")
