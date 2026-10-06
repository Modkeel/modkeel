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

The author's unpublished port beats an old JAR that only passed static checks, which is why
official_source comes before older_official.
"""

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Union

import requests

from modkeel.build import validate_jar
from modkeel.constants import MODRINTH_USER_AGENT
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

SOURCE_ORDER = ["official", "official_source", "older_official", "fork"]

STRATEGY_LABELS = {
    "official": "Official build",
    "official_source": "Author's branch",
    "older_official": "Older official build",
    "fork": "Community fork",
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


def _install(jar: Path, config: ModCompilerConfig) -> Path:
    """Put a JAR in the output directory (and the instance's mods/ when one was given)."""
    dest = config.output_dir / jar.name
    if jar.resolve() != dest.resolve():
        shutil.copy2(jar, dest)
    print(f"    \U0001f4be Saved: {dest}")
    if config.mods_path:
        shutil.copy2(jar, config.mods_path / jar.name)
        print(f"    \U0001f4be Installed: {config.mods_path / jar.name}")
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
        print(f"    \U0001f4e5 Downloading {primary['filename']}...")
        try:
            _download(primary["url"], dest)
        except requests.RequestException as e:
            return Rejected(f"download failed ({e})")
        _install(dest, ctx.config)
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

    def deliver(self, candidate: Candidate, mod: ModRef,
                ctx: ResolveContext) -> Union[Delivered, Rejected]:
        version, built_for = candidate.data["version"], candidate.data["built_for"]
        target = ctx.mc_version
        primary = _primary_file(version)
        if not primary:
            return Rejected("the version has no files")

        with tempfile.TemporaryDirectory(prefix="modkeel_older_") as tmp:
            jar = Path(tmp) / primary["filename"]
            print(f"    \U0001f4e5 Checking {primary['filename']} (built for MC {built_for})...")
            try:
                _download(primary["url"], jar)
            except requests.RequestException as e:
                return Rejected(f"download failed ({e})")

            # Loaders refuse a mod whose declared Minecraft range excludes the game.
            ok, _, _, message = validate_jar(jar, target)
            if not ok:
                return Rejected(message.replace("JAR declares incompatible MC version",
                                                "its metadata only allows MC"))

            rejected = _linkage_rejection(jar, target)
            if rejected:
                return rejected

            dest = _install(jar, ctx.config)

        deps = ctx.modrinth.download_modrinth_deps({"required_deps": _required_deps(version)})
        return Delivered(
            jar_path=dest, mod_name=mod.title,
            mod_version=version.get("version_number", "unknown"),
            evidence=["metadata", "linkage"], dependencies=deps or [],
            caveat=(f"Built for MC {built_for}. Its metadata allows {target} and every "
                    f"Minecraft class it uses exists in {target}, but that is a static "
                    f"check: test it in game (or with --docker-test) before relying on it."),
        )


def _version_key(game_version: str):
    return tuple(int(p) for p in game_version.split("."))


def _linkage_rejection(jar: Path, mc_version: str) -> Optional[Rejected]:
    """None when the JAR's bytecode resolves on mc_version, else why it is rejected.

    Unlike the pipeline's prebuilt check, an inconclusive check rejects: an official build
    for the exact target was not found, so a JAR we cannot verify is not offered as if it
    were one.
    """
    from modkeel.linkage import check_jar
    from modkeel.mappings import load_index

    index = load_index(mc_version)
    if index is None:
        return Rejected(f"cannot verify: no symbol table for MC {mc_version}")
    report = check_jar(jar, index)
    if not report.checked:
        return Rejected(f"cannot verify ({report.skip_reason})")
    if not report.is_clean:
        return Rejected(f"{len(report.missing_classes)} Minecraft classes it uses "
                        f"don't exist in {mc_version}")
    print(f"    ✓ Linkage: {report.summary}")
    return None


# ---------------------------------------------------------------------------
# fork
# ---------------------------------------------------------------------------

def prefilter_forks(github, validator, forks: List[Dict], limit: int = 10,
                    upstream: Optional[tuple] = None,
                    copies: Optional[List[str]] = None) -> List[Dict]:
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
        if compatible:
            fork["_best_branch"] = max(compatible, key=lambda b: validator.score_branch(b))
            validated.append(fork)
    return validated


@contextmanager
def temp_pipeline(config: ModCompilerConfig) -> Iterator["object"]:
    """A Pipeline with its own temporary work dir, removed on exit (success or not).

    For one-off builds (search, get); `compile` uses Pipeline.process_repos, which manages
    its own work dir.
    """
    from modkeel.pipeline import Pipeline

    pipeline = Pipeline(config)
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
        github = GitHubClient(config)
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
        validated = prefilter_forks(github, BranchValidator(github, config), forks,
                                    upstream=upstream, copies=copies)
        if not validated:
            n = len(copies)
            copied = (f"; {n} {'was an unchanged copy' if n == 1 else 'were unchanged copies'}"
                      f" of {owner}/{repo}" if copies else "")
            return Found(note=f"No compatible forks found for MC {ctx.mc_version} + "
                              f"{ctx.loader} ({len(forks)} checked{copied})")
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
        print(f"\n  Best fork: {candidate.label}")
        if not ctx.loader_version:
            return Rejected("-lv LOADER_VERSION is required to compile")

        fork = candidate.data["fork"]
        config = ctx.config_with_token(candidate.data["token"])
        with temp_pipeline(config) as pipeline:
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


STRATEGIES = {cls.name: cls for cls in (OfficialSource, OlderOfficialSource, ForkSource)}


def default_strategies() -> List[SourceStrategy]:
    """The strategies for a mod known by name (get, search), in SOURCE_ORDER."""
    return in_source_order([cls() for cls in STRATEGIES.values()])


def in_source_order(strategies: List[SourceStrategy]) -> List[SourceStrategy]:
    """Sort any set of strategies by SOURCE_ORDER. A command that has no implementation
    of a step (get has no official_source: it knows no repo branches) simply skips it;
    a strategy whose name is not in SOURCE_ORDER is a bug, so it raises."""
    rank = {name: i for i, name in enumerate(SOURCE_ORDER)}
    return sorted(strategies, key=lambda s: rank[s.name])
