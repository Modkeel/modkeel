"""Pipeline orchestrator for Modkeel."""

import io
import logging
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

from modkeel.build import (
    compile_mod,
    create_maven_local_init_script,
    publish_to_maven_local,
    validate_jar,
)
from modkeel.constants import MODRINTH_USER_AGENT
from modkeel.docker import DockerTester
from modkeel.github import GitHubClient, parse_repo_url
from modkeel.models import CompilationResult, FailureType, ModCompilerConfig
from modkeel.modrinth import ModrinthClient
from modkeel.prebuild import PreBuildGate
from modkeel.utils import safe_rmtree
from modkeel.loaders import get_bridge_mods, get_cross_loader_chain, get_profile
from modkeel.validation import BranchValidator
from modkeel.version import is_version_in_maven_range, is_version_in_fabric_range

logger = logging.getLogger("modkeel")


@dataclass
class BranchPlan:
    """Where clone_and_compile builds from: the repo (upstream or a fork), its candidate
    branches best first, whether they target the cross-loader fallback, and the fork search
    results (reused by the cross-loader fallback)."""

    owner: str
    repo: str
    branches: List
    cross_loader: bool = False
    fork_candidates: List[Dict] = field(default_factory=list)


def _age_label(commit_date: Optional[str]) -> str:
    """", 12d ago" / ", 3mo ago" for the candidates list; empty past a year or if unparsable."""
    if not commit_date:
        return ""
    try:
        commit_time = datetime.fromisoformat(commit_date.replace("Z", "+00:00"))
        days = (datetime.now(timezone.utc) - commit_time).days
    except Exception:
        return ""
    if days < 30:
        return f", {days}d ago"
    if days < 365:
        return f", {days // 30}mo ago"
    return ""


class Pipeline:
    """Orchestrates the full mod compilation pipeline."""

    def __init__(self, config: ModCompilerConfig):
        self.config = config
        self.github = GitHubClient(config)
        self.validator = BranchValidator(self.github, config)
        self.modrinth = ModrinthClient(config)
        self.docker = DockerTester(config)
        # getattr: configs built before the symbol check existed (the deprecated shim,
        # partial test doubles) must keep working.
        symbol_check = getattr(config, "symbol_check", True)
        self.prebuild = PreBuildGate(
            self.github, config=config if symbol_check else None
        )
        self.results: List[CompilationResult] = []
        self.temp_dir = None

    def _try_prebuilt(
        self, repo_url: str, owner: str, repo: str, branch
    ) -> Optional[CompilationResult]:
        """Download an already-built JAR instead of compiling, if one exists.

        Returns None whenever nothing usable is found, so the caller falls through to
        the normal build path.
        """
        artifact = self.prebuild.find_prebuilt(
            owner, repo, branch.name, self.config.mc_version
        )
        if not artifact:
            return None

        print(
            f"\n  \U0001f4e5 Found pre-built JAR ({artifact.source}): {artifact.name}"
        )
        print("     Skipping compilation entirely.")

        try:
            headers = self.config.github_headers if artifact.requires_auth else {}
            response = requests.get(artifact.url, headers=headers, timeout=180)
            response.raise_for_status()
            payload = response.content

            if artifact.is_zip:
                jar_name, payload = self._extract_jar_from_zip(payload)
                if not payload:
                    print("    ⚠️  Artifact contained no usable JAR")
                    return None
            else:
                jar_name = artifact.name

            dest = self.config.output_dir / jar_name
            dest.write_bytes(payload)
            print(f"    \U0001f4be Saved: {dest}")

            is_valid, mod_name, mod_version, message = validate_jar(
                dest, self.config.mc_version
            )
            if not is_valid:
                print(f"    ⚠️  Pre-built JAR rejected ({message}), compiling instead")
                dest.unlink(missing_ok=True)
                return None

            # Level 0.5: metadata can claim any version; the bytecode cannot.
            if not self._linkage_ok(dest):
                dest.unlink(missing_ok=True)
                return None

            if self.config.mods_path:
                instance_dest = self.config.mods_path / jar_name
                instance_dest.write_bytes(payload)
                print(f"    \U0001f4be Installed: {instance_dest}")

            print(f"\n  ✅ SUCCESS: {mod_name} v{mod_version} [pre-built, no compile]")

            return CompilationResult(
                repo_url=repo_url,
                success=True,
                branch=branch.name,
                jar_path=str(dest),
                mod_name=mod_name,
                mod_version=mod_version,
                compiled_mc_version=branch.minecraft_version or self.config.mc_version,
            )

        except Exception as e:
            print(f"    ⚠️  Pre-built download failed: {e}")
            return None

    def _linkage_ok(self, jar_path: Path) -> bool:
        """Verify a downloaded JAR's bytecode actually targets the requested version.

        Returns True whenever the check cannot run -- an unreadable naming scheme, no
        symbol table -- so an inconclusive check never costs the user a working JAR.
        """
        if not getattr(self.config, "symbol_check", True):
            return True

        from modkeel.linkage import check_jar
        from modkeel.mappings import load_index

        index = load_index(self.config.mc_version)
        if index is None:
            return True

        report = check_jar(jar_path, index)
        if not report.checked:
            print(f"    \U0001f50e Linkage: {report.summary}")
            return True

        if report.is_clean:
            print(f"    ✓ Linkage: {report.summary}")
            return True

        print(f"    ⚠️  Linkage: {report.summary} -- JAR targets a different version")
        for finding in report.findings[:3]:
            print(f"        - {finding}")
        print("    ℹ️  Rejecting pre-built JAR, compiling instead")
        return False

    @staticmethod
    def _extract_jar_from_zip(payload: bytes):
        """Pull the largest non-sources JAR out of an Actions artifact ZIP."""
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                candidates = [
                    info
                    for info in archive.infolist()
                    if info.filename.lower().endswith(".jar")
                    and not any(
                        s in info.filename.lower()
                        for s in ("sources", "javadoc", "-dev", "-slim")
                    )
                ]
                if not candidates:
                    return None, None
                best = max(candidates, key=lambda i: i.file_size)
                return Path(best.filename).name, archive.read(best)
        except (zipfile.BadZipFile, OSError):
            return None, None

    def clone_and_compile(
        self,
        repo_url: str,
        specific_branch: Optional[str] = None,
        extra_gradle_args: Optional[List[str]] = None,
        skip_modrinth: bool = False,
    ) -> CompilationResult:
        """Produce a JAR for one repository: Modrinth download, prebuilt JAR or local build.

        Stages, each returning early with a CompilationResult when it settles the outcome:
          0. Modrinth: a published build for the target (also via cross-loader fallback).
          1. Branch plan: which repo (upstream or a fork) and which branches to build.
          2. Pre-build gate + prebuilt lookup: skip builds whose outcome is already known.
          3. Build loop: clone, validate, compile and validate the JAR, branch by branch.
        Any unexpected exception becomes a failed result, so one repo never stops a batch.
        """
        print(f"\n{'=' * 80}")
        print(f"\U0001f4e6 Processing: {repo_url}")
        print(f"{'=' * 80}")

        try:
            owner, repo, url_branch = parse_repo_url(repo_url)
            print(f"  \U0001f4cd Repository: {owner}/{repo}")

            if url_branch:
                specific_branch = url_branch
                print(f"  \U0001f33f Using specified branch: {specific_branch}")

            repo_info = self.github.get_repo_info(owner, repo)
            if repo_info:
                stars = repo_info.get("stargazers_count", 0)
                forks = repo_info.get("forks_count", 0)
                print(f"  ⭐ Stars: {stars} | \U0001f374 Forks: {forks}")

            if not specific_branch and not skip_modrinth:
                downloaded = self._try_modrinth(repo_url, owner, repo)
                if downloaded:
                    return downloaded

            # Clones always land in <temp>/<upstream repo name>, even when a fork is built.
            repo_temp_dir = Path(self.temp_dir) / repo

            plan = self._plan_branches(repo_url, owner, repo, specific_branch)
            if isinstance(plan, CompilationResult):
                return plan

            branches = plan.branches
            # Level 1: drop branches with deterministic build failures before cloning
            if self.config.prebuild_gate and branches:
                print(f"\n  ⚡ Pre-build gate ({len(branches)} candidates)...")
                branches = self.prebuild.filter_branches(plan.owner, plan.repo, branches)

            # Level 0: a published JAR means the build already happened elsewhere
            if self.config.use_prebuilt and branches:
                prebuilt_result = self._try_prebuilt(repo_url, plan.owner, plan.repo, branches[0])
                if prebuilt_result:
                    return prebuilt_result

            return self._build_branches(
                repo_url, plan.owner, plan.repo, branches, repo_temp_dir,
                extra_gradle_args, plan.cross_loader,
            )

        except Exception as e:
            return CompilationResult(
                repo_url=repo_url, success=False, error=f"Unexpected error: {e}"
            )

    # ------------------------------------------------------------------
    # Stage 0: Modrinth
    # ------------------------------------------------------------------

    def _try_modrinth(self, repo_url: str, owner: str, repo: str) -> Optional[CompilationResult]:
        """Download the mod from Modrinth when it has a build for the target.

        Returns None (fall through to GitHub) when Modrinth has nothing or the download fails.
        """
        hit = self._find_modrinth_hit(owner, repo)
        if not hit:
            return None

        print("\n  \U0001f4e5 Downloading from Modrinth (no compilation needed)...")
        try:
            dl_resp = requests.get(
                hit["download_url"],
                headers={"User-Agent": MODRINTH_USER_AGENT},
                timeout=120,
            )
            dl_resp.raise_for_status()

            filename = hit["filename"]
            dest = self.config.output_dir / filename
            dest.write_bytes(dl_resp.content)
            print(f"    \U0001f4be Saved: {dest}")

            if self.config.mods_path:
                instance_dest = self.config.mods_path / filename
                instance_dest.write_bytes(dl_resp.content)
                print(f"    \U0001f4be Installed: {instance_dest}")

            self.modrinth.download_modrinth_deps(hit)

            is_cross = hit.get("_cross_loader", False)
            cross_note = " [Fabric via Sinytra Connector]" if is_cross else ""
            print(
                f"\n  ✅ SUCCESS: {hit['title']} "
                f"v{hit['version_number']} "
                f"from Modrinth [pre-compiled]{cross_note}"
            )

            return CompilationResult(
                repo_url=repo_url,
                success=True,
                jar_path=str(dest),
                mod_name=hit["title"],
                mod_version=hit["version_number"],
                compiled_mc_version=self.config.mc_version,
                modrinth_download=True,
                is_cross_loader=is_cross,
            )
        except Exception as e:
            print(f"    ⚠️  Modrinth download failed: {e}")
            print("    ℹ️  Falling back to GitHub compilation...")
            return None

    def _find_modrinth_hit(self, owner: str, repo: str) -> Optional[Dict]:
        """Modrinth result for the target loader, else for a cross-loader fallback loader.

        A fallback hit is tagged `_cross_loader` / `_fallback_loader`. The configured loader is
        swapped only for the duration of each lookup (check_modrinth reads it from config).
        """
        source = f"{owner}/{repo}"
        hit = self.modrinth.check_modrinth(repo, source_repo=source)
        if hit:
            return hit

        fallback_loaders = get_cross_loader_chain(self.config.loader)
        if not (
            self.config.cross_loader
            and fallback_loaders
            and self.modrinth.is_cross_loader_available()
        ):
            return None

        saved_loader = self.config.loader
        try:
            for fallback in fallback_loaders:
                self.config.loader = fallback
                print(
                    f"  \U0001f504 CROSS-LOADER: Checking Modrinth for "
                    f"{fallback.capitalize()} version..."
                )
                hit = self.modrinth.check_modrinth(repo, source_repo=source)
                if hit:
                    hit["_cross_loader"] = True
                    hit["_fallback_loader"] = fallback
                    return hit
        finally:
            self.config.loader = saved_loader
        return None

    # ------------------------------------------------------------------
    # Stage 1: which repo and branches to build
    # ------------------------------------------------------------------

    def _plan_branches(
        self, repo_url: str, owner: str, repo: str, specific_branch: Optional[str]
    ) -> "BranchPlan | CompilationResult":
        """Decide where to build from, or return the failed result that ends the attempt."""
        print("  \U0001f50d Fetching branches...")
        all_branches = self.github.get_branches(owner, repo)

        if not all_branches:
            return CompilationResult(
                repo_url=repo_url,
                success=False,
                error="Could not fetch branches from repository",
            )

        print(f"  \U0001f4ca Found {len(all_branches)} branches")

        if specific_branch:
            return self._plan_specific_branch(
                repo_url, owner, repo, specific_branch, all_branches
            )

        plan = self._discover_branches(owner, repo, all_branches)
        if not plan.branches:
            return self._no_branches_result(repo_url, owner, repo, plan)

        print(f"  \U0001f3af Found {len(plan.branches)} compatible branches")
        self._rank_branches(plan.branches)
        return plan

    def _plan_specific_branch(
        self, repo_url: str, owner: str, repo: str, name: str, all_branches: List
    ) -> "BranchPlan | CompilationResult":
        """The user named the branch: it must exist and pass pre-validation. No ranking."""
        target_branch = next((b for b in all_branches if b.name == name), None)
        if not target_branch:
            return CompilationResult(
                repo_url=repo_url,
                success=False,
                error=f"Specified branch '{name}' not found",
            )

        print("  \U0001f50d Validating specified branch via GitHub API...")
        if not self.validator.pre_validate_branch(owner, repo, target_branch):
            return CompilationResult(
                repo_url=repo_url,
                success=False,
                error=(
                    f"Branch '{name}' is not compatible: {target_branch.validation_error}"
                ),
            )
        return BranchPlan(owner, repo, [target_branch])

    def _discover_branches(self, owner: str, repo: str, all_branches: List) -> "BranchPlan":
        """Compatible branches in the upstream repo, or in a community fork when upstream
        has no exact match. May return an empty plan; fork candidates are kept on it for the
        cross-loader fallback."""
        all_branches = self.validator.filter_branches_by_version_proximity(all_branches)
        compatible = self.validator.pre_validate_branches(owner, repo, all_branches)

        exact_matches = [b for b in compatible if b.minecraft_version == self.config.mc_version]
        close_matches = [b for b in compatible if b.minecraft_version != self.config.mc_version]

        if not compatible:
            should_search_forks = True
        elif not exact_matches and close_matches:
            print(
                f"  ⚠️  Only found close version matches "
                f"({close_matches[0].minecraft_version}), searching for exact "
                f"{self.config.mc_version}..."
            )
            should_search_forks = True
        else:
            should_search_forks = False

        if not should_search_forks:
            return BranchPlan(owner, repo, compatible)

        cross_available = (
            self.config.cross_loader
            and get_cross_loader_chain(self.config.loader)
            and self.modrinth.is_cross_loader_available()
        )
        fork_candidates = self.github.search_compatible_repos(
            owner, repo, cross_loader_available=cross_available
        ) or []

        if fork_candidates:
            print(f"\n  \U0001f3af Trying top {len(fork_candidates)} community forks...")
            for fork_result in fork_candidates:
                fork_branches = self._check_fork(owner, repo, fork_result, close_matches)
                if fork_branches:
                    fork_info = fork_result["fork"]
                    return BranchPlan(fork_info["owner"], fork_info["repo"], fork_branches,
                                      fork_candidates=fork_candidates)

            if close_matches:
                print(
                    "\n  ℹ️  No exact version in forks, using close matches "
                    "from original repo"
                )
                compatible = close_matches

        return BranchPlan(owner, repo, compatible, fork_candidates=fork_candidates)

    def _check_fork(
        self, owner: str, repo: str, fork_result: Dict, upstream_close: List
    ) -> Optional[List]:
        """Branches to build from this fork, or None to try the next fork.

        In order: an exact-version branch; a close branch whose declared range covers the
        target; any compatible branch, but only when upstream had no close match to fall
        back on.
        """
        fork_info = fork_result["fork"]
        fork_owner = fork_info["owner"]
        fork_repo = fork_info["repo"]

        print(f"\n  \U0001f4e6 Checking fork: {fork_info['full_name']}")
        print(
            f"     Score: {fork_result['score']}, "
            f"Signals: {', '.join(fork_result['signals'])}"
        )

        fork_branches = self.github.get_branches(fork_owner, fork_repo)
        fork_compatible = self.validator.pre_validate_branches(
            fork_owner, fork_repo, fork_branches
        )

        fork_exact = [b for b in fork_compatible if b.minecraft_version == self.config.mc_version]
        if fork_exact:
            print(f"  ✅ Found EXACT version {self.config.mc_version} in fork!")
            for fb in fork_exact:
                is_clean, diff_bonus, diff_desc = self.validator.analyze_fork_diff(
                    owner, repo, fork_owner, fork_repo, fb.name
                )
                fb.score += diff_bonus
                if is_clean or diff_bonus > 0:
                    print(f"  \U0001f50d Diff analysis: {diff_desc}")
            self._print_fork_notice(fork_result)
            return fork_exact

        fork_close = [b for b in fork_compatible if b.minecraft_version != self.config.mc_version]
        if fork_close and self._fork_range_covers_target(fork_owner, fork_repo, fork_close):
            print("  ✅ Using fork with validated version range support!")
            return fork_close

        if fork_compatible and not upstream_close:
            print(f"  ✅ Found {len(fork_compatible)} compatible branches in fork!")
            return fork_compatible

        return None

    def _fork_range_covers_target(self, fork_owner: str, fork_repo: str, fork_close: List) -> bool:
        """Whether the best close branch declares a version range covering the target.

        On success every close branch is marked with that range (validation_method
        "fork_metadata_range"), since they share the fork's metadata.
        """
        print(
            f"  ℹ️  No exact match, checking if close matches support "
            f"{self.config.mc_version}..."
        )
        best_branch = fork_close[0]
        version_range = self.validator.parse_version_range_from_metadata(
            fork_owner, fork_repo, best_branch.name, self.config.loader
        )
        if not version_range:
            print("  ⚠️  Could not determine version range from metadata")
            return False

        if get_profile(self.config.loader)["version_range_format"] == "maven":
            is_compat = is_version_in_maven_range(self.config.mc_version, version_range)
        else:
            is_compat = is_version_in_fabric_range(self.config.mc_version, version_range)

        if not is_compat:
            print(f"  ❌ Range {version_range} does not cover {self.config.mc_version}")
            return False

        print(f"  ✅ Fork branch '{best_branch.name}' supports range {version_range}")
        print(f"     → Covers target {self.config.mc_version}!")
        for b in fork_close:
            b.version_range = version_range
            b.validation_method = "fork_metadata_range"
        return True

    @staticmethod
    def _print_fork_notice(fork_result: Dict) -> None:
        """Security notice shown before building code from a community fork."""
        fork_info = fork_result["fork"]
        trust_score = fork_result.get("trust_score", 50)
        trust_analysis = fork_info.get("trust_analysis", {})

        print("\n  ⚠️  SECURITY NOTICE: Using community fork (not official)")
        print(f"      Trust Score: {trust_score}% - ", end="")
        if trust_score >= 80:
            print("HIGH confidence (established contributors)")
        elif trust_score >= 60:
            print("MEDIUM confidence (some established contributors)")
        elif trust_score >= 40:
            print("LOW confidence (new/unknown contributors)")
        else:
            print("CRITICAL - Multiple red flags detected")

        if trust_analysis.get("warnings"):
            print("      Warnings:")
            for warning in trust_analysis["warnings"]:
                print(f"      - {warning}")
        if trust_analysis.get("signals"):
            print(f"      Signals: {', '.join(trust_analysis['signals'])}")

        print(f"      Review fork: {fork_info['url']}")
        print(f"      Compiling code from: {fork_info['owner']}")

    def _no_branches_result(
        self, repo_url: str, owner: str, repo: str, plan: "BranchPlan"
    ) -> "BranchPlan | CompilationResult":
        """Nothing compatible for the target loader: try the cross-loader fallback (Fabric
        branches run through bridge mods), else return the failure."""
        fallback_loaders = get_cross_loader_chain(self.config.loader)
        base_error = (
            f"No compatible branches in original repo "
            f"or forks for MC {self.config.mc_version}"
            f" + {self.config.loader}"
        )
        if not (
            self.config.cross_loader
            and fallback_loaders
            and self.modrinth.is_cross_loader_available()
        ):
            return CompilationResult(repo_url=repo_url, success=False, error=base_error)

        print(
            f"\n  \U0001f504 CROSS-LOADER: No {self.config.loader.capitalize()} branches found, "
            f"trying {fallback_loaders[0].capitalize()} fallback via bridge mods..."
        )
        for fallback_loader in fallback_loaders:
            found = self._cross_loader_branches(owner, repo, plan.fork_candidates,
                                                fallback_loader)
            if found:
                fb_owner, fb_repo, fb_branches = found
                print(
                    f"  ✅ Found {len(fb_branches)} {fallback_loader.capitalize()} "
                    f"branches for cross-loader compilation"
                )
                print(f"  \U0001f3af Found {len(fb_branches)} compatible branches")
                self._rank_branches(fb_branches)
                return BranchPlan(fb_owner, fb_repo, fb_branches, cross_loader=True)

        tried = ", ".join(fallback_loaders)
        return CompilationResult(
            repo_url=repo_url,
            success=False,
            error=f"{base_error} (also tried {tried} cross-loader fallback)",
        )

    def _cross_loader_branches(
        self, owner: str, repo: str, fork_candidates: List[Dict], loader: str
    ) -> Optional[tuple]:
        """(owner, repo, branches) for `loader` in upstream, else in the first fork that has
        them; None when neither does."""
        branches = self.validator.pre_validate_branches(
            owner, repo, self.github.get_branches(owner, repo), override_loader=loader
        )
        if branches:
            return owner, repo, branches

        for fork_result in fork_candidates:
            fi = fork_result["fork"]
            print(
                f"  \U0001f504 Checking fork {fi['full_name']} "
                f"for {loader.capitalize()} branches..."
            )
            branches = self.validator.pre_validate_branches(
                fi["owner"], fi["repo"], self.github.get_branches(fi["owner"], fi["repo"]),
                override_loader=loader,
            )
            if branches:
                return fi["owner"], fi["repo"], branches
        return None

    def _rank_branches(self, branches: List) -> None:
        """Score and sort branches in place (best first) and print the top three."""
        for b in branches:
            b.score = self.validator.score_branch(b)
        branches.sort(key=lambda b: b.score, reverse=True)

        print("\n  \U0001f4cb Top candidates:")
        for i, b in enumerate(branches[:3], 1):
            exact_indicator = "✓" if b.minecraft_version == self.config.mc_version else "~"
            print(
                f"    {i}. {b.name} (MC {b.minecraft_version} {exact_indicator}, "
                f"score: {b.score}{_age_label(b.commit_date)})"
            )

    # ------------------------------------------------------------------
    # Stage 3: clone, validate, compile
    # ------------------------------------------------------------------

    def _build_branches(
        self,
        repo_url: str,
        owner: str,
        repo: str,
        branches: List,
        repo_temp_dir: Path,
        extra_gradle_args: Optional[List[str]],
        is_cross_loader: bool,
    ) -> CompilationResult:
        """Try branches in order; the first that clones, validates, builds and yields a valid
        JAR wins. Otherwise a failure carrying the last failure type and missing deps (which
        drive process_repos' mavenLocal retry)."""
        branch_errors = []
        last_fail_type = FailureType.UNKNOWN
        last_missing_deps: List[str] = []
        last_fail_clone_dir: Optional[Path] = None

        for i, branch in enumerate(branches, 1):
            print(f"\n  \U0001f33f Attempting [{i}/{len(branches)}]: {branch.name}")
            version_match = (
                "exact" if branch.minecraft_version == self.config.mc_version else "close"
            )
            print(
                f"     MC: {branch.minecraft_version} ({version_match}), "
                f"Loader: {branch.loader} {branch.loader_version}"
            )

            clone_error = self._clone(owner, repo, branch.name, repo_temp_dir)
            if clone_error:
                print(f"    ❌ {clone_error}")
                branch_errors.append(f"{branch.name}: {clone_error}")
                last_fail_type = FailureType.CLONE_ERROR
                continue

            print("    \U0001f50d Validating gradle.properties...")
            is_valid, message = self.validator.validate_gradle_properties(
                repo_temp_dir, skip_loader_validation=is_cross_loader
            )
            if not is_valid:
                print(f"    ❌ {message}")
                branch_errors.append(f"{branch.name}: {message}")
                last_fail_type = FailureType.VALIDATION_ERROR
                continue
            print(f"    {message}")

            print("    \U0001f50d Validating build.gradle...")
            is_valid, message = self.validator.validate_build_gradle(repo_temp_dir)
            if not is_valid:
                print(f"    ❌ {message}")
                branch_errors.append(f"{branch.name}: {message}")
                last_fail_type = FailureType.VALIDATION_ERROR
                continue
            print(f"    ✅ {message}")

            success, jar_path, message, fail_type, missing_deps = compile_mod(
                repo_temp_dir, extra_gradle_args, self.config.mc_version
            )
            if not success:
                print(f"    ❌ {message}")
                branch_errors.append(f"{branch.name}: {message[:200]}")
                last_fail_type = fail_type
                last_missing_deps = missing_deps
                last_fail_clone_dir = repo_temp_dir
                continue
            print(f"    ✅ {message}")

            print("    \U0001f50d Validating JAR...")
            is_valid, mod_name, mod_version, message = validate_jar(
                jar_path, self.config.mc_version
            )
            if not is_valid:
                print(f"    ❌ {message}")
                branch_errors.append(f"{branch.name}: JAR validation: {message}")
                last_fail_type = FailureType.VALIDATION_ERROR
                continue
            print(f"    ✅ {message}")
            print(f"    \U0001f4cb Mod: {mod_name} v{mod_version}")

            dest_path = self._install_jar(jar_path)

            version_note = ""
            if branch.minecraft_version != self.config.mc_version:
                version_note = f" (compiled for MC {branch.minecraft_version})"
            cross_note = " [Fabric via Sinytra Connector]" if is_cross_loader else ""
            print(
                f"\n  ✅ SUCCESS: {mod_name} v{mod_version} from branch "
                f"'{branch.name}'{version_note}{cross_note}"
            )

            return CompilationResult(
                repo_url=repo_url,
                success=True,
                branch=branch.name,
                jar_path=str(dest_path),
                mod_name=mod_name,
                mod_version=mod_version,
                compiled_mc_version=branch.minecraft_version,
                clone_dir=repo_temp_dir,
                is_cross_loader=is_cross_loader,
            )

        error_detail = f"All {len(branches)} branches failed:\n"
        for err in branch_errors:
            error_detail += f"  - {err}\n"
        return CompilationResult(
            repo_url=repo_url,
            success=False,
            error=error_detail.strip(),
            failure_type=last_fail_type,
            missing_dependencies=last_missing_deps,
            clone_dir=last_fail_clone_dir,
        )

    @staticmethod
    def _clone(owner: str, repo: str, branch_name: str, dest: Path) -> Optional[str]:
        """Shallow-clone one branch into dest (replacing it). Returns an error or None."""
        if dest.exists():
            shutil.rmtree(dest)

        clone_url = f"https://github.com/{owner}/{repo}.git"
        print("    \U0001f4e5 Cloning...")
        try:
            result = subprocess.run(
                ["git", "clone", "-b", branch_name, "--depth", "1", clone_url, str(dest)],
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            return "Clone timeout"
        except Exception as e:
            return f"Clone error: {e}"
        if result.returncode != 0:
            return f"Clone failed: {result.stderr.strip()[:200]}"
        return None

    def _install_jar(self, jar_path: Path) -> Path:
        """Copy a built JAR to the output dir (and the instance's mods dir, if any)."""
        dest_path = self.config.output_dir / jar_path.name
        shutil.copy2(jar_path, dest_path)
        print(f"    \U0001f4be Saved to: {dest_path}")

        if self.config.mods_path:
            instance_dest = self.config.mods_path / jar_path.name
            shutil.copy2(jar_path, instance_dest)
            print(f"    \U0001f4be Installed to: {instance_dest}")
        return dest_path

    def process_repos(self, repo_urls: List[str]):
        """Process a list of repository URLs with dependency-aware multi-pass."""
        self.temp_dir = tempfile.mkdtemp(prefix="mod_compiler_")
        print(f"\U0001f5c2\ufe0f  Using temporary directory: {self.temp_dir}")

        try:
            print(f"\n{'=' * 80}")
            print(f"\U0001f4cb PASS 1: Compiling {len(repo_urls)} repositories")
            print(f"{'=' * 80}")

            pass1_results: Dict[str, CompilationResult] = {}
            for repo_url in repo_urls:
                try:
                    result = self.clone_and_compile(repo_url)
                except Exception as e:
                    logger.error(f"Unhandled error processing {repo_url}: {e}")
                    result = CompilationResult(
                        repo_url=repo_url, success=False, error=f"Unhandled error: {e}"
                    )
                pass1_results[repo_url] = result

                if result.success and result.clone_dir and result.clone_dir.exists():
                    publish_to_maven_local(result.clone_dir)

                time.sleep(1)

            dep_failures = [
                url
                for url, r in pass1_results.items()
                if not r.success and r.failure_type == FailureType.DEPENDENCY_RESOLUTION
            ]

            if dep_failures:
                modrinth_only = [
                    url
                    for url, r in pass1_results.items()
                    if r.success and r.modrinth_download
                ]
                if modrinth_only:
                    print(f"\n{'=' * 80}")
                    print(
                        f"\U0001f4e4 MAVEN PUBLISH: Compiling {len(modrinth_only)} "
                        f"Modrinth-downloaded mods for mavenLocal"
                    )
                    print(f"{'=' * 80}")

                    for repo_url in modrinth_only:
                        print(f"\n  \U0001f4e4 Compiling for mavenLocal: {repo_url}")
                        try:
                            compile_result = self.clone_and_compile(
                                repo_url, skip_modrinth=True
                            )
                            if (
                                compile_result.success
                                and compile_result.clone_dir
                                and compile_result.clone_dir.exists()
                            ):
                                publish_to_maven_local(compile_result.clone_dir)
                        except Exception as e:
                            print(f"    \u26a0\ufe0f  Maven publish failed: {e}")

                        time.sleep(1)

            if dep_failures:
                print(f"\n{'=' * 80}")
                print(
                    f"\U0001f504 PASS 2: Retrying {len(dep_failures)} repos with "
                    f"dependency failures (mavenLocal injection)"
                )
                print(f"{'=' * 80}")

                init_script = create_maven_local_init_script(self.temp_dir)
                maven_args = ["--init-script", str(init_script)]

                for repo_url in dep_failures:
                    prev = pass1_results[repo_url]
                    print(f"\n  \U0001f504 Retrying: {repo_url}")
                    if prev.missing_dependencies:
                        print(
                            f"     Previously missing: "
                            f"{', '.join(prev.missing_dependencies)}"
                        )

                    try:
                        result = self.clone_and_compile(
                            repo_url, extra_gradle_args=maven_args
                        )
                    except Exception as e:
                        logger.error(f"Unhandled error retrying {repo_url}: {e}")
                        result = CompilationResult(
                            repo_url=repo_url,
                            success=False,
                            error=f"Unhandled error (pass 2): {e}",
                        )

                    pass1_results[repo_url] = result

                    if (
                        result.success
                        and result.clone_dir
                        and result.clone_dir.exists()
                    ):
                        publish_to_maven_local(result.clone_dir, maven_args)

                    time.sleep(1)

            self.results = list(pass1_results.values())

            cross_loader_mods = [
                r for r in self.results if r.success and r.is_cross_loader
            ]
            if cross_loader_mods:
                fallback_loaders_dl = get_cross_loader_chain(self.config.loader)
                print(f"\n{'=' * 80}")
                print(
                    f"\U0001f504 CROSS-LOADER: {len(cross_loader_mods)} mod(s) "
                    f"need bridge mods to run on {self.config.loader.capitalize()}"
                )
                print(f"{'=' * 80}")
                for target in fallback_loaders_dl:
                    bridge_slugs = get_bridge_mods(self.config.loader, target)
                    if bridge_slugs:
                        print(
                            f"  Downloading bridge mods for {target.capitalize()} compatibility..."
                        )
                        for slug in bridge_slugs:
                            self.modrinth.download_modrinth_mod(
                                slug, self.config.mc_version, self.config.loader
                            )

            if self.config.docker_test:
                self.docker.test_mods_in_docker(self.results)

        finally:
            print("\n\U0001f9f9 Cleaning up temporary directory...")
            if os.path.exists(self.temp_dir):
                safe_rmtree(Path(self.temp_dir))

    def generate_report(self) -> str:
        """Generate a detailed report of compilation results."""
        report_lines = []
        report_lines.append("\n" + "=" * 80)
        report_lines.append("\U0001f4ca COMPILATION REPORT")
        report_lines.append("=" * 80)

        successful = [r for r in self.results if r.success]
        failed = [r for r in self.results if not r.success]
        version_mismatches = [
            r
            for r in successful
            if r.compiled_mc_version and r.compiled_mc_version != self.config.mc_version
        ]
        cross_loader_mods = [r for r in successful if r.is_cross_loader]

        report_lines.append(
            f"\n\u2705 Successful: {len(successful)}/{len(self.results)}"
        )
        report_lines.append(f"\u274c Failed: {len(failed)}/{len(self.results)}")
        if version_mismatches:
            report_lines.append(
                f"\u26a0\ufe0f  Version warnings: {len(version_mismatches)}"
            )
        if cross_loader_mods:
            report_lines.append(
                f"\U0001f504 Cross-loader (Fabric via Connector): {len(cross_loader_mods)}"
            )

        if successful:
            report_lines.append("\n" + "-" * 80)
            report_lines.append("\u2705 SUCCESSFULLY COMPILED MODS:")
            report_lines.append("-" * 80)

            for result in successful:
                report_lines.append(f"\n\U0001f4e6 {result.repo_url}")
                report_lines.append(f"   \U0001f33f Branch: {result.branch}")
                report_lines.append(
                    f"   \U0001f4cb Mod: {result.mod_name} v{result.mod_version}"
                )

                if result.compiled_mc_version == self.config.mc_version:
                    report_lines.append(
                        f"   \u2705 Version: {result.compiled_mc_version} (exact match)"
                    )
                else:
                    report_lines.append(
                        f"   \u26a0\ufe0f  Version: {result.compiled_mc_version} (target was {self.config.mc_version})"
                    )

                if result.is_cross_loader:
                    report_lines.append(
                        "   \U0001f504 Fabric mod via Sinytra Connector"
                    )

                report_lines.append(f"   \U0001f4be JAR: {result.jar_path}")

        if version_mismatches:
            report_lines.append("\n" + "-" * 80)
            report_lines.append("\u26a0\ufe0f  VERSION WARNINGS:")
            report_lines.append("-" * 80)
            report_lines.append(
                "Some mods were compiled for slightly different Minecraft versions."
            )
            report_lines.append(
                "These will likely work, but TEST IN-GAME before using in production:"
            )
            report_lines.append("")

            for result in version_mismatches:
                report_lines.append(
                    f"  \u2022 {result.mod_name} v{result.mod_version}: Compiled for {result.compiled_mc_version} (you're using {self.config.mc_version})"
                )

            report_lines.append("")
            report_lines.append("To require exact version matches, use --strict flag.")

        if failed:
            report_lines.append("\n" + "-" * 80)
            report_lines.append("\u274c FAILED COMPILATIONS:")
            report_lines.append("-" * 80)

            for result in failed:
                report_lines.append(f"\n\U0001f4e6 {result.repo_url}")
                report_lines.append(f"   \u274c Error: {result.error}")
                if result.failure_type == FailureType.DEPENDENCY_RESOLUTION:
                    report_lines.append("   \U0001f517 Type: Unresolved dependencies")
                    if result.missing_dependencies:
                        for dep in result.missing_dependencies:
                            report_lines.append(f"      - {dep}")

        if cross_loader_mods:
            report_lines.append("\n" + "-" * 80)
            report_lines.append(
                "\U0001f504 CROSS-LOADER MODS (Fabric via Sinytra Connector):"
            )
            report_lines.append("-" * 80)
            report_lines.append(
                "These Fabric mods were compiled because no NeoForge version was found."
            )
            report_lines.append(
                "They require Sinytra Connector + Forgified Fabric API to run on NeoForge."
            )
            report_lines.append(
                "Compatibility is ~85% - some mods may have issues. TEST IN-GAME."
            )
            report_lines.append("")
            for result in cross_loader_mods:
                report_lines.append(
                    f"  \u2022 {result.mod_name} v{result.mod_version} ({result.repo_url})"
                )
            report_lines.append("")
            report_lines.append("Sinytra Connector: https://modrinth.com/mod/connector")
            report_lines.append(
                "Forgified Fabric API: https://modrinth.com/mod/forgified-fabric-api"
            )

        docker_tested = [r for r in self.results if r.docker_tested]
        if docker_tested:
            docker_passed = [r for r in docker_tested if r.docker_test_passed is True]
            docker_failed = [r for r in docker_tested if r.docker_test_passed is False]
            docker_inconclusive = [
                r for r in docker_tested if r.docker_test_passed is None
            ]
            report_lines.append("\n" + "-" * 80)
            report_lines.append("\U0001f433 DOCKER TEST RESULTS:")
            report_lines.append("-" * 80)
            docker_client_only = [
                r
                for r in docker_inconclusive
                if r.docker_error and "[CLIENT-ONLY]" in r.docker_error
            ]
            docker_loader_err = [
                r for r in docker_inconclusive if r not in docker_client_only
            ]
            parts = [
                f"Tested: {len(docker_tested)}",
                f"Passed: {len(docker_passed)}",
                f"Failed: {len(docker_failed)}",
            ]
            if docker_client_only:
                parts.append(f"Client-only: {len(docker_client_only)}")
            if docker_loader_err:
                parts.append(f"Inconclusive: {len(docker_loader_err)}")
            report_lines.append(f"  {'  |  '.join(parts)}")
            if docker_client_only:
                report_lines.append("")
                for r in docker_client_only:
                    label = r.mod_name or r.repo_url
                    report_lines.append(
                        f"  \u2139\ufe0f  {label}: client-only mod, "
                        f"cannot test on headless server"
                    )
            if docker_loader_err:
                report_lines.append("")
                report_lines.append(
                    "  \u26a0\ufe0f  LOADER/INFRASTRUCTURE ERROR (not caused by mods):"
                )
                report_lines.append(f"     {docker_loader_err[0].docker_error}")
            if docker_failed:
                report_lines.append("")
                for r in docker_failed:
                    label = r.mod_name or r.repo_url
                    report_lines.append(f"  \u274c {label}: {r.docker_error}")

        report_lines.append("\n" + "=" * 80)
        report_lines.append(
            f"\U0001f3af Target: Minecraft {self.config.mc_version} with {self.config.loader.capitalize()} {self.config.loader_version}"
        )
        if self.config.strict_version:
            report_lines.append("\U0001f512 Mode: STRICT (exact version matches only)")
        else:
            report_lines.append(
                "\U0001f513 Mode: LENIENT (allows same major.minor versions)"
            )
        report_lines.append(f"\U0001f4c1 Output: {self.config.output_dir}")
        if self.config.mods_path:
            report_lines.append(f"\U0001f4c1 Instance Mods: {self.config.mods_path}")
        report_lines.append("=" * 80)

        return "\n".join(report_lines)
