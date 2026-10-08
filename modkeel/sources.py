"""The strategies of the resolver's source layer, and the order they run in.

See modkeel/resolve.py for the contract. SOURCE_ORDER is the single place that decides what
is tried first; each strategy only knows how to find and deliver its own kind of JAR:

  official        the mod's own published build for the exact target
  official_source the author's own branch for the target, prebuilt or compiled (compile
                  only: it needs the repo; implemented in modkeel/pipeline.py)
  older_official  the mod's own build for an older Minecraft version that still runs on the
                  target: its metadata must allow the target (loaders enforce it) and every
                  Minecraft class its bytecode uses must exist there (modkeel/linkage.py)
  fork            a community fork or port, compiled (or prebuilt)
  relaxed_official an older official build whose metadata excludes the target but whose
                  bytecode resolves on it: its Minecraft range is rewritten (modkeel/relax.py)
                  and it is delivered marked as modified by Modkeel. Last: the author did not
                  declare this version, so anything built or published for it comes first.

The author's unpublished port beats an old JAR that only passed static checks, which is why
official_source comes before older_official.
"""

import shutil
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple, Union

import requests

from modkeel.build import validate_jar
from modkeel.constants import MODKEEL_HOME, MODRINTH_USER_AGENT
from modkeel.core.events import (
    CheckRan,
    Downloading,
    Emitter,
    ForkChosen,
    RangeRelaxed,
    Saved,
)
from modkeel.core.text import print_event
from modkeel.evidence import Evidence, Subject, gather
from modkeel.loaders import get_profile
from modkeel.models import ModCompilerConfig
from modkeel.modrinth import ModrinthClient, pick_version
from modkeel.resolve import (
    Candidate,
    Delivered,
    Found,
    ModRef,
    Rejected,
    ResolveContext,
    SourceStrategy,
)
from modkeel.version import compare_versions

# validate_jar's message when a JAR's declared Minecraft range excludes the target
RANGE_REFUSAL = "JAR declares incompatible MC version"

SOURCE_ORDER = ["official", "official_source", "older_official", "fork", "relaxed_official"]

STRATEGY_LABELS = {
    "official": "Official build",
    "official_source": "Author's branch",
    "older_official": "Older official build",
    "fork": "Community fork",
    "relaxed_official": "Official build, relaxed",
}


def strategy_label(name: str) -> str:
    """Human name of a source strategy for trails ("older_official" -> "Older ...")."""
    return STRATEGY_LABELS.get(name, name.replace("_", " ").capitalize())


def caveat_after_docker(caveat: str, mc_version: str) -> str:
    """A delivery's caveat once a headless server booted with the JAR: the server check
    replaces the "test it" advice; client-side code is still untested."""
    return (f"{caveat.split('. ')[0]}. A headless MC {mc_version} server booted with it "
            "(--docker-test); client-side features are not covered by that test.")


# How many older Minecraft versions older_official tries (nearest first). Each try downloads
# one JAR, so this bounds the cost when none of them runs on the target.
MAX_VERSIONS_BACK = 3

# How many pre-validated forks the fork strategy builds before giving up (each can take
# several minutes).
MAX_FORK_BUILDS = 2


def identify_mod(query: str, modrinth: ModrinthClient) -> ModRef:
    """Who the user means by `query`: the Modrinth project, its GitHub repo, near misses."""
    project, others = modrinth.find_project(query)
    mod = ModRef(query=query, project=project, related=others,
                 lookup_error=modrinth.last_error)
    if project:
        full = modrinth.fetch_project(project.get("project_id") or project["slug"])
        if full:
            mod.source_repo = ModrinthClient.source_repo_of(full)
    return mod


def identify_repo(owner: str, repo: str, modrinth: ModrinthClient) -> ModRef:
    """The mod behind a GitHub repo (compile's input): its Modrinth project when one names
    that repo as its source, which older_official needs."""
    mod = ModRef(query=repo, source_repo=f"{owner}/{repo}")
    try:
        mod.project = modrinth.find_project_by_repo(owner, repo)
    except Exception as e:  # identity is a bonus for compile: never fail the repo on it
        mod.lookup_error = str(e)
    return mod


def _display(loader: str) -> str:
    return get_profile(loader)["display_name"]


def _primary_file(version: Dict) -> Optional[Dict]:
    files = version.get("files", [])
    return next((f for f in files if f.get("primary")), files[0] if files else None)


def _required_deps(version: Dict) -> List[str]:
    return [
        d["project_id"] for d in version.get("dependencies", [])
        if d.get("dependency_type") == "required" and d.get("project_id")
    ]


def _download(url: str, dest: Path) -> None:
    resp = requests.get(url, headers={"User-Agent": MODRINTH_USER_AGENT}, timeout=120)
    resp.raise_for_status()
    dest.write_bytes(resp.content)


def _cached_download(url: str, filename: str) -> Path:
    """A Modrinth file in ~/.modkeel/cache/downloads (downloaded once per filename).

    older_official and relaxed_official check the same older builds; the cache keeps the
    second from downloading them again (Modrinth filenames carry the version).
    """
    cache = MODKEEL_HOME / "cache" / "downloads"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / filename
    if not (path.exists() and path.stat().st_size > 0):
        partial = path.with_suffix(path.suffix + ".part")
        _download(url, partial)
        partial.replace(path)
    return path


def _install(jar: Path, config: ModCompilerConfig, events: Emitter = print_event) -> Path:
    """Put a JAR in the output directory (and the instance's mods/ when one was given)."""
    dest = config.output_dir / jar.name
    if jar.resolve() != dest.resolve():
        shutil.copy2(jar, dest)
    events(Saved(dest))
    if config.mods_path:
        shutil.copy2(jar, config.mods_path / jar.name)
        events(Saved(config.mods_path / jar.name, installed=True))
    return dest


def _release_like(game_version: str) -> bool:
    """1.21.10 or 26.2, not 25w14a or 1.21.11-rc1: only these compare numerically."""
    return all(part.isdigit() for part in game_version.split("."))


def _family(game_version: str) -> str:
    """Versions close enough to try each other's builds: 1.21.x for 1.x, 26.x for year-based.

    Across families the game changes too much for an unchanged JAR to run, so builds from
    another family are not worth downloading.
    """
    parts = game_version.split(".")
    return ".".join(parts[:2]) if parts[0] == "1" else parts[0]


# ---------------------------------------------------------------------------
# official
# ---------------------------------------------------------------------------

class OfficialSource(SourceStrategy):
    name = "official"
    label = "Official build"
    cheap = True

    def check(self, candidate: Candidate, mod: ModRef,
              ctx: ResolveContext) -> Optional[Rejected]:
        """The author's build listed for the exact target is its own evidence."""
        return None if _primary_file(candidate.data["version"]) else Rejected(
            "the version has no files")

    def find(self, mod: ModRef, ctx: ResolveContext) -> Found:
        if not mod.project:
            if mod.lookup_error:
                return Found(note=f"Modrinth unavailable ({mod.lookup_error})")
            return Found(note="Not found on Modrinth")

        versions = ctx.modrinth.project_versions(
            mod.project["project_id"], ctx.loader, ctx.mc_version)
        if versions is None:
            return Found(note=f"Modrinth unavailable ({ctx.modrinth.last_error})")
        if not versions:
            return Found(note=f"{mod.title} has no {_display(ctx.loader)} build "
                              f"for MC {ctx.mc_version}")
        version = pick_version(versions)
        return Found([Candidate(
            label=f"{mod.title} {version.get('version_number', '?')} "
                  f"({version.get('version_type', 'release')})",
            data={"version": version},
        )])

    def deliver(self, candidate: Candidate, mod: ModRef,
                ctx: ResolveContext) -> Union[Delivered, Rejected]:
        version = candidate.data["version"]
        primary = _primary_file(version)
        if not primary:
            return Rejected("the version has no files")
        dest = ctx.config.output_dir / primary["filename"]
        ctx.events(Downloading(primary["filename"]))
        try:
            _download(primary["url"], dest)
        except requests.RequestException as e:
            return Rejected(f"download failed ({e})")
        _install(dest, ctx.config, ctx.events)
        deps = ctx.modrinth.download_modrinth_deps({"required_deps": _required_deps(version)})
        return Delivered(
            jar_path=dest, mod_name=mod.title,
            mod_version=version.get("version_number", "unknown"),
            evidence=["published"], dependencies=deps or [],
        )


# ---------------------------------------------------------------------------
# older_official
# ---------------------------------------------------------------------------

class OlderOfficialSource(SourceStrategy):
    name = "older_official"
    label = "Older official build"
    cheap = True

    def find(self, mod: ModRef, ctx: ResolveContext) -> Found:
        if not mod.project:
            return Found(note="needs the mod's Modrinth project")
        target = ctx.mc_version
        if not _release_like(target):
            return Found(note=f"MC {target} is not a release version")

        versions = ctx.modrinth.project_versions(mod.project["project_id"], ctx.loader)
        if versions is None:
            return Found(note=f"Modrinth unavailable ({ctx.modrinth.last_error})")

        # Newest build per older Minecraft version of the same family, nearest first.
        by_mc: Dict[str, List[Dict]] = {}
        for version in versions:
            supported = [g for g in version.get("game_versions", []) if _release_like(g)]
            if not supported or target in version.get("game_versions", []):
                continue
            newest = max(supported, key=_version_key)
            if _family(newest) == _family(target) and compare_versions(newest, target) < 0:
                by_mc.setdefault(newest, []).append(version)

        nearest = sorted(by_mc, key=_version_key, reverse=True)[:MAX_VERSIONS_BACK]
        if not nearest:
            return Found(note=f"no older {_display(ctx.loader)} builds in the "
                              f"{_family(target)} line")
        return Found([
            Candidate(
                label=f"{pick_version(by_mc[mc]).get('version_number', '?')} for MC {mc}",
                data={"version": pick_version(by_mc[mc]), "built_for": mc},
            )
            for mc in nearest
        ])

    def check(self, candidate: Candidate, mod: ModRef,
              ctx: ResolveContext) -> Optional[Rejected]:
        judged = self._judge(candidate, mod, ctx)
        return judged if isinstance(judged, Rejected) else None

    def _judge(self, candidate: Candidate, mod: ModRef,
               ctx: ResolveContext) -> Union[Tuple[Path, Evidence], Rejected]:
        """Download the candidate (cached) and judge it on the target, installing nothing.

        deliver() and check() share it, so the target layer's proposal (which asks
        check() through Resolver.would_resolve) uses exactly the evidence that would
        deliver the JAR there.
        """
        version, built_for = candidate.data["version"], candidate.data["built_for"]
        primary = _primary_file(version)
        if not primary:
            return Rejected("the version has no files")

        ctx.events(Downloading(primary["filename"], purpose="check", built_for=built_for))
        try:
            jar = _cached_download(primary["url"], primary["filename"])
        except requests.RequestException as e:
            return Rejected(f"download failed ({e})")

        # Loaders refuse a mod whose declared Minecraft range excludes the game; then its
        # bytecode must resolve there. Both are required: an unverifiable JAR is not offered.
        # Mixins are judged too; a mixin check that cannot run does not reject (linkage
        # already needs the same symbol tables), one that finds a broken injection does.
        evidence = gather(Subject(jar, ctx.mc_version, built_for, mod.title), ctx.config,
                          ["metadata", "linkage", "mixins"], required=["metadata", "linkage"],
                          events=ctx.events)
        if not evidence.ok:
            return Rejected(evidence.reason)
        return jar, evidence

    def deliver(self, candidate: Candidate, mod: ModRef,
                ctx: ResolveContext) -> Union[Delivered, Rejected]:
        version, built_for = candidate.data["version"], candidate.data["built_for"]
        target = ctx.mc_version
        judged = self._judge(candidate, mod, ctx)
        if isinstance(judged, Rejected):
            return judged
        jar, evidence = judged
        _print_static(evidence, ctx.events)

        dest = _install(jar, ctx.config, ctx.events)

        deps = ctx.modrinth.download_modrinth_deps({"required_deps": _required_deps(version)})
        return Delivered(
            jar_path=dest, mod_name=mod.title,
            mod_version=version.get("version_number", "unknown"),
            evidence=evidence.passed, dependencies=deps or [],
            caveat=(f"Built for MC {built_for}. Its metadata allows {target}, every "
                    f"Minecraft class it uses exists in {target}, no method or field it "
                    f"calls was removed or renamed since {built_for} and its mixins still "
                    f"find their targets, but that is a static "
                    f"check: test it in game (or with --docker-test) before relying on it."),
        )


class RelaxedOfficialSource(OlderOfficialSource):
    """Older official builds refused only by their declared range, range rewritten.

    Same candidates as older_official (nearest older builds of the line, cached download).
    A build is relaxed only when its metadata is what excludes the target and its bytecode
    resolves on the target (linkage on classes and members, inconclusive rejects); a build
    whose metadata already allows the target was older_official's to judge.

    A relaxed build is delivered only after a headless server boots with it: the author
    closed the range on purpose, and static checks cannot see a mixin whose target method
    changed body. (Before members were checked, TorchMaster 21.8.2 on 1.21.9 passed linkage
    and crashed on the renamed BlockBehaviour$Properties.noCollission(); the member check
    now rejects it before any download of a server.) Without Docker, or for a client-only
    mod a server cannot load, it is rejected as unverified.
    """

    cheap = False  # a relaxed JAR needs a server boot: never sized up on other versions


    name = "relaxed_official"
    label = "Official build, relaxed"

    def deliver(self, candidate: Candidate, mod: ModRef,
                ctx: ResolveContext) -> Union[Delivered, Rejected]:
        from modkeel.relax import relax_jar

        version, built_for = candidate.data["version"], candidate.data["built_for"]
        target = ctx.mc_version
        primary = _primary_file(version)
        if not primary:
            return Rejected("the version has no files")
        try:
            jar = _cached_download(primary["url"], primary["filename"])
        except requests.RequestException as e:
            return Rejected(f"download failed ({e})")

        ok, _, _, message = validate_jar(jar, target, min_size=0)
        if ok:
            return Rejected("its metadata already allows the target (older_official's case)")
        if RANGE_REFUSAL not in message:
            # Only a declared range is rewritten; anything else stays a refusal
            return Rejected(f"refused for something other than its range ({message})")
        static = gather(Subject(jar, target, built_for, mod.title), ctx.config,
                        ["linkage", "mixins"], required=["linkage"], events=ctx.events)
        if not static.ok:
            return Rejected(static.reason)
        _print_static(static, ctx.events)

        stem = primary["filename"].removesuffix(".jar")
        dest = ctx.config.output_dir / f"{stem}+modkeel-relaxed-mc{target}.jar"
        try:
            change = relax_jar(jar, dest, built_for, target)
        except (zipfile.BadZipFile, OSError, ValueError) as e:  # ValueError: broken JSON
            dest.unlink(missing_ok=True)
            return Rejected(f"its metadata could not be rewritten ({e})")
        if change is None:
            return Rejected("no Minecraft range in its metadata to rewrite")
        ctx.events(RangeRelaxed(change.metadata_file, change.old_range, change.new_range))

        # The rewritten JAR must now pass metadata, and a server must boot with it: no
        # Docker or an inconclusive boot is a rejection here, not a skipped check.
        deps = ctx.modrinth.download_modrinth_deps({"required_deps": _required_deps(version)})
        runtime = gather(Subject(dest, target, built_for, mod.title, deps or []), ctx.config,
                         ["metadata", "docker_server"], events=ctx.events)
        if not runtime.ok:
            dest.unlink(missing_ok=True)
            failure = runtime.failure
            if failure.check == "metadata":
                return Rejected(f"still refused after rewriting its range ({failure.detail})")
            return Rejected(failure.detail)
        _install(dest, ctx.config, ctx.events)
        return Delivered(
            jar_path=dest, mod_name=mod.title,
            mod_version=version.get("version_number", "unknown"),
            evidence=[*static.passed, "metadata_relaxed", *runtime.passed],
            dependencies=deps or [],
            caveat=(f"Built for MC {built_for}; its author declared {change.old_range}. "
                    f"Modkeel added {target} to that range ({change.new_range}) because every "
                    f"Minecraft class and member it uses exists in {target} and a headless {target} "
                    f"server booted with it; client-side features are untested, so try it in "
                    f"a test world first. The file name and META-INF/modkeel-relaxed.txt mark "
                    f"it as modified."),
        )


def _version_key(game_version: str):
    return tuple(int(p) for p in game_version.split("."))


def _print_static(evidence, events: Emitter = print_event) -> None:
    """Report what the static checks that passed covered (how much was checked)."""
    for outcome in evidence.outcomes:
        if outcome.passed and outcome.check in ("linkage", "mixins"):
            events(CheckRan(outcome.check, outcome.status, outcome.detail))


def possible_ports(fork_name: str, branches: List) -> List[str]:
    """Branches pre-validation rejected only because they declare another Minecraft range.

    Free (the branches are already judged). From a fork with commits of its own they may
    hold real porting work, but their JAR would declare that range and the loader refuse
    it, so they are listed for the trail ("possible ports, not tried"), never built.
    """
    return [
        f"{fork_name} {b.name} (declares {b.version_range or '?'})"
        for b in branches
        if not b.is_compatible and b.validation_error
        and ("declared range" in b.validation_error or " not in range " in b.validation_error)
    ]


def possible_ports_note(found: List[str], shown: int = 3) -> str:
    """'; possible ports not tried ...: a, b, c (+2 more)' for a trail note, or ''."""
    if not found:
        return ""
    more = f" (+{len(found) - shown} more)" if len(found) > shown else ""
    return ("; possible ports not tried (commits of their own, but they declare another "
            f"Minecraft range): {', '.join(found[:shown])}{more}")


def prefilter_forks(github, validator, forks: List[Dict], limit: int = 10,
                    upstream: Optional[tuple] = None,
                    copies: Optional[List[str]] = None,
                    possible: Optional[List[str]] = None) -> List[Dict]:
    """Forks (of the first `limit`) with at least one pre-validated branch.

    Each kept fork gets its best-scoring branch under "_best_branch". Only the GitHub API is
    used (gradle.properties and friends); nothing is cloned. With upstream
    (owner, repo, branches), branches that are unchanged copies of upstream ones are dropped
    before pre-validation, and forks made only of copies are named in `copies`.
    """
    from modkeel.github import never_pushed, split_unchanged_branches

    validated = []
    for fork in forks[:limit]:
        fork_info = fork["fork"]
        if never_pushed(fork_info):
            if copies is not None:
                copies.append(fork_info["full_name"])
            continue
        branches = github.get_branches(fork_info["owner"], fork_info["repo"])
        if branches and upstream and upstream[2]:
            branches, unchanged = split_unchanged_branches(github, *upstream, branches)
            if unchanged and not branches and copies is not None:
                copies.append(fork_info["full_name"])
        if not branches:
            continue
        compatible = validator.pre_validate_branches(
            fork_info["owner"], fork_info["repo"], branches
        )
        if possible is not None:
            possible += possible_ports(fork_info["full_name"], branches)
        if compatible:
            if len(compatible) > 1:  # dates rank branches; one candidate needs none
                github.fill_commit_dates(fork_info["owner"], fork_info["repo"], compatible)
            fork["_best_branch"] = max(compatible, key=lambda b: validator.score_branch(b))
            validated.append(fork)
    return validated


@contextmanager
def temp_pipeline(config: ModCompilerConfig,
                  events: Emitter = print_event) -> Iterator["object"]:
    """A Pipeline with its own temporary work dir, removed on exit (success or not).

    For one-off builds (search, get); `compile` uses Pipeline.process_repos, which manages
    its own work dir.
    """
    from modkeel.pipeline import Pipeline

    pipeline = Pipeline(config, events)
    pipeline.temp_dir = tempfile.mkdtemp(prefix="mod_compiler_")
    try:
        yield pipeline
    finally:
        if pipeline.temp_dir:
            shutil.rmtree(pipeline.temp_dir, ignore_errors=True)


class ForkSource(SourceStrategy):
    name = "fork"
    label = "Community fork"
    max_attempts = MAX_FORK_BUILDS

    def find(self, mod: ModRef, ctx: ResolveContext) -> Found:
        from modkeel.github import GitHubClient
        from modkeel.validation import BranchValidator

        token = ctx.github_token()
        if not token:
            return Found(note="needs a GitHub token: modkeel token --set ghp_YOUR_TOKEN")

        config = ctx.config_with_token(token)
        github = GitHubClient(config, ctx.events)
        # Forks of the mod's own repo when Modrinth names it, else repos named like the query
        owner, repo = (mod.source_repo.split("/", 1) if mod.source_repo
                       else (mod.query, mod.query))
        forks = github.search_compatible_repos(owner, repo, False)
        if not forks:
            if github.search_denied:
                return Found(note=f"GitHub refused {github.search_denied} of the searches "
                                  "(rate limit or no access): forks unknown")
            return Found(note="No GitHub forks found")

        # Forks of the mod's own repo can be compared with it: unchanged copies are skipped
        upstream = (owner, repo, github.get_branches(owner, repo)) if mod.source_repo else None
        copies: List[str] = []
        possible: List[str] = []
        validated = prefilter_forks(github, BranchValidator(github, config, ctx.events), forks,
                                    upstream=upstream, copies=copies, possible=possible)
        if not validated:
            n = len(copies)
            copied = (f"; {n} {'was an unchanged copy' if n == 1 else 'were unchanged copies'}"
                      f" of {owner}/{repo}" if copies else "")
            return Found(note=f"No compatible forks found for MC {ctx.mc_version} + "
                              f"{ctx.loader} ({len(forks)} checked{copied})"
                              f"{possible_ports_note(possible)}")
        return Found(
            [
                Candidate(
                    label=f"{fork['fork']['full_name']} branch {fork['_best_branch'].name} "
                          f"(MC {fork['_best_branch'].minecraft_version or '?'})",
                    data={"fork": fork, "token": token},
                )
                for fork in validated
            ],
            note=f"{len(validated)} of {len(forks)} passed",
        )

    def deliver(self, candidate: Candidate, mod: ModRef,
                ctx: ResolveContext) -> Union[Delivered, Rejected]:
        ctx.events(ForkChosen(candidate.label))
        if not ctx.loader_version:
            return Rejected("-lv LOADER_VERSION is required to compile")

        fork = candidate.data["fork"]
        config = ctx.config_with_token(candidate.data["token"])
        with temp_pipeline(config, ctx.events) as pipeline:
            result = pipeline.clone_and_compile(
                f"https://github.com/{fork['fork']['full_name']}",
                specific_branch=fork["_best_branch"].name,
                skip_modrinth=True,
            )
        if not result.success:
            return Rejected(f"compilation failed: {result.error}")
        return Delivered(
            jar_path=Path(result.jar_path) if result.jar_path else config.output_dir,
            mod_name=result.mod_name or mod.title,
            mod_version=result.mod_version or "unknown",
            verb="compiled",
            evidence=["built"],
            caveat=f"Community build from {fork['fork']['full_name']}, not the author's.",
        )


STRATEGIES = {cls.name: cls for cls in (OfficialSource, OlderOfficialSource, ForkSource,
                                         RelaxedOfficialSource)}


def default_strategies() -> List[SourceStrategy]:
    """The strategies for a mod known by name (get, search), in SOURCE_ORDER."""
    return in_source_order([cls() for cls in STRATEGIES.values()])


def in_source_order(strategies: List[SourceStrategy]) -> List[SourceStrategy]:
    """Sort any set of strategies by SOURCE_ORDER. A command that has no implementation
    of a step (get has no official_source: it knows no repo branches) simply skips it;
    a strategy whose name is not in SOURCE_ORDER is a bug, so it raises."""
    rank = {name: i for i, name in enumerate(SOURCE_ORDER)}
    return sorted(strategies, key=lambda s: rank[s.name])
