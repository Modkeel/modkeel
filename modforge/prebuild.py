"""Pre-build gating: decide whether a branch is worth compiling before paying for a build.

Two levels, both cheap and both running over HTTP without cloning:

Level 0 (``PrebuiltFinder``) -- somebody already paid the compute. GitHub Releases assets
and successful Actions runs are evidence of a real build, not a prediction of one. When a
release asset exists the build can be skipped entirely.

Level 1 (``StaticBuildCheck``) -- deterministic environment failures. A dead Maven
repository, a Gradle wrapper too old for the local JDK, or a missing wrapper all fail the
build with certainty. Detecting them costs a handful of GETs instead of ten minutes of
Gradle.

Neither level predicts Java compilation errors (changed APIs, missing symbols); that is
irreducible without a compiler. Both levels target environment failures, which dominate
long-tail forks.
"""

import logging
import re
import shutil
import tempfile
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

logger = logging.getLogger("modforge")

# Sentinel distinguishing "probe the local machine" from an explicit unknown JDK.
AUTO_DETECT_JDK = object()


# ── Gradle / JDK compatibility ───────────────────────────────────────────────

# Minimum Gradle version able to run on a given JDK major version. A Gradle older than
# this fails at daemon startup with "Unsupported class file major version".
MIN_GRADLE_FOR_JDK: Dict[int, Tuple[int, int]] = {
    8: (2, 0),
    11: (5, 0),
    16: (7, 0),
    17: (7, 3),
    18: (7, 5),
    19: (7, 6),
    20: (8, 3),
    21: (8, 5),
    22: (8, 8),
    23: (8, 10),
    24: (8, 14),
    25: (9, 0),
}

# Build plugins that cannot run on modern Gradle/JDK combinations.
LEGACY_PLUGIN_PATTERNS: List[Tuple[str, str, str]] = [
    (
        r"net\.minecraftforge\.gradle[^\n]*?version\s*[:=]\s*['\"]([0-9.]+)",
        "3.0",
        "ForgeGradle {found} requires JDK 8 and Gradle 4.x-6.x",
    ),
    (
        r"['\"]net\.minecraftforge\.gradle:ForgeGradle:([0-9.]+)",
        "3.0",
        "ForgeGradle {found} requires JDK 8 and Gradle 4.x-6.x",
    ),
]

# Maven hosts that are permanently gone. A build declaring one of these as a repository
# cannot resolve dependencies from it.
DEAD_MAVEN_HOSTS: Dict[str, str] = {
    "jcenter.bintray.com": "JCenter was shut down in 2021",
    "dl.bintray.com": "Bintray was shut down in 2021",
    "jfrog.bintray.com": "Bintray was shut down in 2021",
    "maven.bintray.com": "Bintray was shut down in 2021",
    "files.minecraftforge.net": "Forge Maven moved to maven.minecraftforge.net",
    "maven.jei.jeed.pw": "Host no longer resolves",
}

# Repository URLs appear inside `repositories { ... }` blocks in several shapes:
#   maven { url = 'https://...' }   maven { url uri("https://...") }   maven("https://...")
REPO_URL_PATTERN = re.compile(r"""["'](https?://[^"'\s]+)["']""")


@dataclass
class PrebuiltArtifact:
    """A compiled JAR that already exists, so no build is required."""

    source: str  # 'release' | 'ci_artifact'
    name: str
    url: str
    tag: Optional[str] = None
    requires_auth: bool = False
    is_zip: bool = False

    def __repr__(self) -> str:
        return f"PrebuiltArtifact(source={self.source}, name={self.name})"


@dataclass
class CIStatus:
    """What a repository's CI says about whether its branch builds."""

    has_workflows: bool = False
    last_conclusion: Optional[str] = None  # 'success' | 'failure' | None
    run_url: Optional[str] = None

    @property
    def score_delta(self) -> int:
        """Score adjustment reflecting CI evidence."""
        if self.last_conclusion == "success":
            return 150
        if self.last_conclusion == "failure":
            return -100
        if not self.has_workflows:
            return -25
        return 0

    @property
    def summary(self) -> str:
        if self.last_conclusion == "success":
            return "CI green on this branch"
        if self.last_conclusion == "failure":
            return "CI red on this branch"
        if not self.has_workflows:
            return "no CI configured"
        return "CI configured but no run for this branch"


@dataclass
class PreBuildVerdict:
    """Outcome of the Level 1 static checks for a single branch."""

    blocking: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    checks_run: int = 0

    @property
    def will_fail(self) -> bool:
        """True when a deterministic failure was found; compiling is wasted work."""
        return bool(self.blocking)

    @property
    def summary(self) -> str:
        if self.blocking:
            return "; ".join(self.blocking)
        if self.warnings:
            return "; ".join(self.warnings)
        return f"{self.checks_run} checks passed"


# ── Local JDK detection ──────────────────────────────────────────────────────


_jdk_major_cache: Optional[int] = None
_jdk_probed = False


def detect_local_jdk() -> Optional[int]:
    """Return the major version of the JDK on PATH, or None if undetectable."""
    global _jdk_major_cache, _jdk_probed

    if _jdk_probed:
        return _jdk_major_cache

    _jdk_probed = True
    try:
        result = subprocess.run(
            ["java", "-version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None

    # `java -version` writes to stderr on most JDKs, stdout on some.
    output = f"{result.stderr}\n{result.stdout}"
    match = re.search(r'version\s+"(\d+)(?:\.(\d+))?', output)
    if not match:
        return None

    major = int(match.group(1))
    # Legacy scheme: 1.8.0_402 means Java 8.
    if major == 1 and match.group(2):
        major = int(match.group(2))

    _jdk_major_cache = major
    return major


def parse_gradle_version(distribution_url: str) -> Optional[Tuple[int, int]]:
    """Extract (major, minor) from a Gradle distributionUrl."""
    match = re.search(r"gradle-(\d+)\.(\d+)", distribution_url)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def gradle_supports_jdk(
    gradle_version: Tuple[int, int], jdk_major: int
) -> Tuple[bool, str]:
    """Check a Gradle version against a JDK major version."""
    # Pick the highest table entry at or below the JDK in use, so unknown future JDKs
    # inherit the newest known requirement instead of silently passing.
    applicable = [jdk for jdk in MIN_GRADLE_FOR_JDK if jdk <= jdk_major]
    if not applicable:
        return True, ""

    required = MIN_GRADLE_FOR_JDK[max(applicable)]
    if gradle_version >= required:
        return True, ""

    return False, (
        f"Gradle {gradle_version[0]}.{gradle_version[1]} cannot run on JDK {jdk_major} "
        f"(needs >= {required[0]}.{required[1]})"
    )


def extract_repository_urls(build_gradle: str) -> List[str]:
    """Extract Maven repository URLs declared in a build script."""
    urls = []
    for match in REPO_URL_PATTERN.finditer(build_gradle):
        url = match.group(1)
        # Skip non-repository URLs that commonly appear in build scripts.
        if any(part in url for part in ("github.com", "://www.", "schema", ".html")):
            continue
        if url not in urls:
            urls.append(url)
    return urls


def check_maven_host_alive(url: str, timeout: int = 5) -> Tuple[bool, str]:
    """Check whether a Maven repository host is reachable.

    Only DNS/connection failures and HTTP 410 count as dead. A 404 on the repository
    root is normal for many Maven hosts and must not be treated as a failure.
    """
    host = urlparse(url).netloc.lower()

    for dead_host, reason in DEAD_MAVEN_HOSTS.items():
        if host == dead_host or host.endswith(f".{dead_host}"):
            return False, f"{host}: {reason}"

    try:
        response = requests.head(url, timeout=timeout, allow_redirects=True)
    except requests.ConnectionError:
        return False, f"{host}: host unreachable (DNS or connection failure)"
    except requests.RequestException:
        return True, ""  # Timeouts are inconclusive, not proof of death.

    if response.status_code == 410:
        return False, f"{host}: returns HTTP 410 Gone"

    return True, ""


class PrebuiltFinder:
    """Level 0: find evidence that a build already happened."""

    def __init__(self, github_client):
        self.github = github_client
        self.config = github_client.config

    def _get(self, url: str, timeout: int = 10) -> Optional[object]:
        try:
            response = requests.get(
                url, headers=self.config.github_headers, timeout=timeout
            )
            if response.status_code != 200:
                return None
            return response.json()
        except (requests.RequestException, ValueError):
            return None

    def find_release_jar(
        self, owner: str, repo: str, mc_version: Optional[str] = None
    ) -> Optional[PrebuiltArtifact]:
        """Find a published JAR asset in the repository's releases."""
        releases = self._get(
            f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=30"
        )
        if not releases:
            return None

        best: Optional[PrebuiltArtifact] = None
        best_size = -1

        for release in releases:
            if release.get("draft"):
                continue

            tag = release.get("tag_name", "")
            for asset in release.get("assets", []):
                name = asset.get("name", "")
                lowered = name.lower()

                if not lowered.endswith(".jar"):
                    continue
                if any(s in lowered for s in ("sources", "javadoc", "-dev", "-slim")):
                    continue
                if mc_version and mc_version not in f"{name} {tag}":
                    continue

                size = asset.get("size", 0)
                if size > best_size:
                    best_size = size
                    best = PrebuiltArtifact(
                        source="release",
                        name=name,
                        url=asset.get("browser_download_url", ""),
                        tag=tag,
                    )

            if best is not None:
                # Releases come back newest-first; stop at the newest matching one.
                break

        return best

    def get_ci_status(self, owner: str, repo: str, branch: str) -> CIStatus:
        """Report what CI says about this branch."""
        workflows = self._get(
            f"https://api.github.com/repos/{owner}/{repo}/actions/workflows"
        )
        has_workflows = bool(workflows and workflows.get("total_count", 0) > 0)

        if not has_workflows:
            return CIStatus(has_workflows=False)

        runs = self._get(
            f"https://api.github.com/repos/{owner}/{repo}/actions/runs"
            f"?branch={branch}&per_page=10"
        )
        if not runs or not runs.get("workflow_runs"):
            return CIStatus(has_workflows=True)

        for run in runs["workflow_runs"]:
            conclusion = run.get("conclusion")
            if conclusion in ("success", "failure"):
                return CIStatus(
                    has_workflows=True,
                    last_conclusion=conclusion,
                    run_url=run.get("html_url"),
                )

        return CIStatus(has_workflows=True)

    def find_ci_artifact(
        self, owner: str, repo: str, branch: str
    ) -> Optional[PrebuiltArtifact]:
        """Find a build artifact from the newest successful Actions run on a branch.

        Actions artifacts expire (90 days by default), require authentication, and are
        served as ZIP archives, so callers must unpack them.
        """
        runs = self._get(
            f"https://api.github.com/repos/{owner}/{repo}/actions/runs"
            f"?branch={branch}&status=success&per_page=5"
        )
        if not runs or not runs.get("workflow_runs"):
            return None

        for run in runs["workflow_runs"]:
            artifacts = self._get(
                f"https://api.github.com/repos/{owner}/{repo}"
                f"/actions/runs/{run['id']}/artifacts"
            )
            if not artifacts:
                continue

            for artifact in artifacts.get("artifacts", []):
                if artifact.get("expired"):
                    continue
                return PrebuiltArtifact(
                    source="ci_artifact",
                    name=artifact.get("name", "artifact"),
                    url=artifact.get("archive_download_url", ""),
                    requires_auth=True,
                    is_zip=True,
                )

        return None


class StaticBuildCheck:
    """Level 1: deterministic environment failures, detected without cloning."""

    def __init__(self, github_client, jdk_major=AUTO_DETECT_JDK):
        self.github = github_client
        # `None` explicitly means "unknown JDK"; the sentinel means "probe the machine".
        self.jdk_major = (
            detect_local_jdk() if jdk_major is AUTO_DETECT_JDK else jdk_major
        )

    def check(self, owner: str, repo: str, branch: str) -> PreBuildVerdict:
        """Run every Level 1 check against a branch."""
        verdict = PreBuildVerdict()

        self._check_gradle_wrapper(owner, repo, branch, verdict)
        self._check_maven_repositories(owner, repo, branch, verdict)
        self._check_legacy_plugins(owner, repo, branch, verdict)

        return verdict

    def _check_gradle_wrapper(
        self, owner: str, repo: str, branch: str, verdict: PreBuildVerdict
    ) -> None:
        verdict.checks_run += 1

        content = self.github.get_file_from_repo(
            owner, repo, branch, "gradle/wrapper/gradle-wrapper.properties"
        )
        if not content:
            # An unreachable branch returns None for every file. Only call the wrapper
            # missing when the branch demonstrably has a build script.
            has_build_script = any(
                self.github.get_file_from_repo(owner, repo, branch, name)
                for name in ("build.gradle", "build.gradle.kts", "settings.gradle")
            )
            if has_build_script:
                verdict.blocking.append(
                    "no Gradle wrapper (gradle-wrapper.properties missing)"
                )
            else:
                verdict.warnings.append("branch has no readable build files")
            return

        match = re.search(r"distributionUrl\s*=\s*(.+)", content)
        if not match:
            verdict.warnings.append("wrapper present but distributionUrl unreadable")
            return

        gradle_version = parse_gradle_version(match.group(1))
        if not gradle_version:
            verdict.warnings.append("could not parse Gradle version from wrapper")
            return

        if self.jdk_major is None:
            verdict.warnings.append(
                f"Gradle {gradle_version[0]}.{gradle_version[1]} (local JDK unknown)"
            )
            return

        supported, reason = gradle_supports_jdk(gradle_version, self.jdk_major)
        if not supported:
            verdict.blocking.append(reason)

    def _check_maven_repositories(
        self, owner: str, repo: str, branch: str, verdict: PreBuildVerdict
    ) -> None:
        verdict.checks_run += 1

        content = self.github.get_file_from_repo(owner, repo, branch, "build.gradle")
        if not content:
            content = self.github.get_file_from_repo(
                owner, repo, branch, "build.gradle.kts"
            )
        if not content:
            return

        for url in extract_repository_urls(content):
            alive, reason = check_maven_host_alive(url)
            if not alive:
                verdict.blocking.append(f"dead Maven repository -- {reason}")

    def _check_legacy_plugins(
        self, owner: str, repo: str, branch: str, verdict: PreBuildVerdict
    ) -> None:
        verdict.checks_run += 1

        if self.jdk_major is None or self.jdk_major <= 8:
            return

        content = self.github.get_file_from_repo(owner, repo, branch, "build.gradle")
        if not content:
            return

        for pattern, max_version, template in LEGACY_PLUGIN_PATTERNS:
            match = re.search(pattern, content)
            if not match:
                continue

            found = match.group(1)
            try:
                found_parts = tuple(int(p) for p in found.split(".")[:2])
                max_parts = tuple(int(p) for p in max_version.split(".")[:2])
            except ValueError:
                continue

            if found_parts <= max_parts:
                verdict.warnings.append(
                    template.format(found=found) + f", local JDK is {self.jdk_major}"
                )


class SymbolChecker:
    """Level 1.5: verify Minecraft symbols exist at the target version, without compiling.

    Costs one tarball download plus a cached mappings lookup, so it runs only on the top
    few surviving candidates. See docs/symbol-check-design.md.
    """

    BUILD_FILES = (
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "gradle.properties",
        "gradle/libs.versions.toml",
    )

    def __init__(self, github_client, config):
        self.github = github_client
        self.config = config

    def check(self, owner: str, repo: str, branch: str):
        """Return a SymbolReport for a branch. Never raises."""
        from modforge.javascan import fetch_source_tree, scan_tree
        from modforge.mappings import detect_flavor, load_index
        from modforge.symbols import SymbolReport

        scripts = [
            self.github.get_file_from_repo(owner, repo, branch, name)
            for name in self.BUILD_FILES
        ]
        flavor = detect_flavor(scripts, loader=self.config.loader)

        index = load_index(self.config.mc_version, flavor)
        if index is None:
            return SymbolReport.skipped(
                f"no symbol table for {self.config.mc_version} ({flavor})"
            )

        temp_dir = Path(tempfile.mkdtemp(prefix="modforge-symbols-"))
        try:
            root = fetch_source_tree(owner, repo, branch, temp_dir)
            if root is None:
                return SymbolReport.skipped("source tarball unavailable")

            from modforge.symbols import check_references

            return check_references(scan_tree(root), index)
        except Exception as e:  # noqa: BLE001 - advisory layer must never break a build
            logger.debug("symbol check failed for %s/%s@%s: %s", owner, repo, branch, e)
            return SymbolReport.skipped(f"symbol check error: {e}")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class PreBuildGate:
    """Runs Level 0 and Level 1 over branch candidates before any build is attempted."""

    # Each symbol check downloads a source tarball, so it is bounded to the best few
    # candidates. Anything dropped is logged rather than silently ignored.
    SYMBOL_CHECK_LIMIT = 3

    def __init__(self, github_client, jdk_major=AUTO_DETECT_JDK, config=None):
        self.finder = PrebuiltFinder(github_client)
        self.checker = StaticBuildCheck(github_client, jdk_major=jdk_major)
        self.symbols = SymbolChecker(github_client, config) if config else None

    def find_prebuilt(
        self, owner: str, repo: str, branch: str, mc_version: Optional[str] = None
    ) -> Optional[PrebuiltArtifact]:
        """Level 0: return an already-built JAR if one exists."""
        artifact = self.finder.find_release_jar(owner, repo, mc_version)
        if artifact:
            return artifact
        return self.finder.find_ci_artifact(owner, repo, branch)

    def _apply_symbol_checks(
        self, owner: str, repo: str, survivors: List, verbose: bool
    ) -> None:
        """Score the top candidates by symbol resolution, then re-sort in place.

        Advisory only: a symbol finding never removes a candidate, it only reorders.
        """
        if self.symbols is None or not survivors:
            return

        checked = survivors[: self.SYMBOL_CHECK_LIMIT]
        if verbose and len(survivors) > len(checked):
            print(
                f"  \U0001f50e Symbol check on top {len(checked)} of "
                f"{len(survivors)} candidates"
            )

        for branch in checked:
            report = self.symbols.check(owner, repo, branch.name)
            branch.symbol_report = report
            branch.score += report.score_delta

            if verbose and report.checked:
                mark = "✓" if report.is_clean else "⚠"
                print(f"     {mark} {branch.name}: {report.summary}")
                for finding in report.findings[:3]:
                    print(f"        - {finding}")

        survivors.sort(key=lambda b: b.score, reverse=True)

    def filter_branches(
        self, owner: str, repo: str, branches: List, verbose: bool = True
    ) -> List:
        """Drop branches with deterministic failures and reorder the rest by CI evidence.

        Returns the surviving branches, best first. Branches are never all dropped: if
        every candidate fails a Level 1 check the original list is returned unchanged,
        since a wrong static check must not make a mod unbuildable.
        """
        if not branches:
            return branches

        survivors = []
        rejected = []

        for branch in branches:
            verdict = self.checker.check(owner, repo, branch.name)
            branch.prebuild_verdict = verdict

            if verdict.will_fail:
                rejected.append((branch, verdict))
                continue

            ci = self.finder.get_ci_status(owner, repo, branch.name)
            branch.ci_status = ci
            branch.score += ci.score_delta
            survivors.append(branch)

        if verbose and rejected:
            print(
                f"  ⚡ Pre-build gate rejected {len(rejected)} branch(es) without building:"
            )
            for branch, verdict in rejected:
                print(f"     ✗ {branch.name}: {verdict.summary}")

        if not survivors:
            if verbose:
                print("  ⚠️  All branches failed the pre-build gate; trying them anyway")
            return branches

        survivors.sort(key=lambda b: b.score, reverse=True)
        self._apply_symbol_checks(owner, repo, survivors, verbose)

        if verbose:
            for branch in survivors[:3]:
                ci = getattr(branch, "ci_status", None)
                if ci and ci.last_conclusion:
                    print(f"     ✓ {branch.name}: {ci.summary} (score {branch.score})")

        return survivors
