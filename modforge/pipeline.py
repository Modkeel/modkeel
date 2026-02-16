"""Pipeline orchestrator for ModForge."""

import logging
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

from modforge.build import (
    classify_build_failure,
    compile_mod,
    create_maven_local_init_script,
    publish_to_maven_local,
    validate_jar,
)
from modforge.constants import MODRINTH_USER_AGENT
from modforge.docker import DockerTester
from modforge.github import GitHubClient, parse_repo_url
from modforge.models import CompilationResult, FailureType, ModCompilerConfig
from modforge.modrinth import ModrinthClient
from modforge.utils import safe_rmtree
from modforge.validation import BranchValidator
from modforge.version import is_version_in_maven_range, is_version_in_fabric_range

logger = logging.getLogger("modforge")


class Pipeline:
    """Orchestrates the full mod compilation pipeline."""

    def __init__(self, config: ModCompilerConfig):
        self.config = config
        self.github = GitHubClient(config)
        self.validator = BranchValidator(self.github, config)
        self.modrinth = ModrinthClient(config)
        self.docker = DockerTester(config)
        self.results: List[CompilationResult] = []
        self.temp_dir = None

    def clone_and_compile(
        self, repo_url: str, specific_branch: Optional[str] = None,
        extra_gradle_args: Optional[List[str]] = None,
        skip_modrinth: bool = False
    ) -> CompilationResult:
        """Clone a repository, find compatible branch, compile, and validate."""
        print(f"\n{'='*80}")
        print(f"\U0001f4e6 Processing: {repo_url}")
        print(f"{'='*80}")

        is_cross_loader_attempt = False
        saved_fork_candidates = []

        try:
            owner, repo, url_branch = parse_repo_url(repo_url)
            print(f"  \U0001f4cd Repository: {owner}/{repo}")

            if url_branch:
                specific_branch = url_branch
                print(f"  \U0001f33f Using specified branch: {specific_branch}")

            repo_info = self.github.get_repo_info(owner, repo)
            if repo_info:
                stars = repo_info.get('stargazers_count', 0)
                forks = repo_info.get('forks_count', 0)
                print(f"  \u2b50 Stars: {stars} | \U0001f374 Forks: {forks}")

            # Step 0: Check Modrinth
            if not specific_branch and not skip_modrinth:
                modrinth_result = self.modrinth.check_modrinth(repo)

                if (not modrinth_result
                        and self.config.cross_loader
                        and self.config.loader in ("neoforge", "forge")
                        and self.modrinth.is_cross_loader_available()):
                    saved_loader = self.config.loader
                    self.config.loader = "fabric"
                    print(f"  \U0001f504 CROSS-LOADER: Checking Modrinth for Fabric version...")
                    modrinth_result = self.modrinth.check_modrinth(repo)
                    self.config.loader = saved_loader
                    if modrinth_result:
                        modrinth_result["_cross_loader"] = True

                if modrinth_result:
                    print(f"\n  \U0001f4e5 Downloading from Modrinth (no compilation needed)...")
                    try:
                        dl_resp = requests.get(
                            modrinth_result["download_url"],
                            headers={"User-Agent": MODRINTH_USER_AGENT},
                            timeout=120
                        )
                        dl_resp.raise_for_status()

                        filename = modrinth_result["filename"]
                        dest = self.config.output_dir / filename
                        dest.write_bytes(dl_resp.content)
                        print(f"    \U0001f4be Saved: {dest}")

                        if self.config.mods_path:
                            instance_dest = self.config.mods_path / filename
                            instance_dest.write_bytes(dl_resp.content)
                            print(f"    \U0001f4be Installed: {instance_dest}")

                        self.modrinth.download_modrinth_deps(modrinth_result)

                        is_cross = modrinth_result.get("_cross_loader", False)
                        cross_note = " [Fabric via Sinytra Connector]" if is_cross else ""
                        print(f"\n  \u2705 SUCCESS: {modrinth_result['title']} "
                              f"v{modrinth_result['version_number']} "
                              f"from Modrinth [pre-compiled]{cross_note}")

                        return CompilationResult(
                            repo_url=repo_url,
                            success=True,
                            jar_path=str(dest),
                            mod_name=modrinth_result["title"],
                            mod_version=modrinth_result["version_number"],
                            compiled_mc_version=self.config.mc_version,
                            modrinth_download=True,
                            is_cross_loader=is_cross,
                        )
                    except Exception as e:
                        print(f"    \u26a0\ufe0f  Modrinth download failed: {e}")
                        print(f"    \u2139\ufe0f  Falling back to GitHub compilation...")

            # Step 1+: GitHub fork search + compilation
            repo_temp_dir = Path(self.temp_dir) / repo

            print(f"  \U0001f50d Fetching branches...")
            all_branches = self.github.get_branches(owner, repo)

            if not all_branches:
                return CompilationResult(
                    repo_url=repo_url,
                    success=False,
                    error="Could not fetch branches from repository"
                )

            print(f"  \U0001f4ca Found {len(all_branches)} branches")

            if specific_branch:
                target_branch = next((b for b in all_branches if b.name == specific_branch), None)
                if not target_branch:
                    return CompilationResult(
                        repo_url=repo_url,
                        success=False,
                        error=f"Specified branch '{specific_branch}' not found"
                    )

                print(f"  \U0001f50d Validating specified branch via GitHub API...")
                is_compatible = self.validator.pre_validate_branch(owner, repo, target_branch)

                if not is_compatible:
                    return CompilationResult(
                        repo_url=repo_url,
                        success=False,
                        error=f"Branch '{specific_branch}' is not compatible: {target_branch.validation_error}"
                    )

                branches_to_try = [target_branch]
            else:
                all_branches = self.validator.filter_branches_by_version_proximity(all_branches)
                compatible_branches = self.validator.pre_validate_branches(owner, repo, all_branches)

                exact_matches = [b for b in compatible_branches if b.minecraft_version == self.config.mc_version]
                close_matches = [b for b in compatible_branches if b.minecraft_version != self.config.mc_version]

                if not compatible_branches:
                    should_search_forks = True
                elif not exact_matches and close_matches:
                    print(f"  \u26a0\ufe0f  Only found close version matches ({close_matches[0].minecraft_version}), searching for exact {self.config.mc_version}...")
                    should_search_forks = True
                else:
                    should_search_forks = False

                if should_search_forks:
                    cross_available = (
                        self.config.cross_loader
                        and self.config.loader in ("neoforge", "forge")
                        and self.modrinth.is_cross_loader_available()
                    )
                    fork_candidates = self.github.search_compatible_repos(
                        owner, repo, cross_loader_available=cross_available
                    )
                    saved_fork_candidates = fork_candidates or []

                    if not fork_candidates:
                        pass
                    else:
                        print(f"\n  \U0001f3af Trying top {len(fork_candidates)} community forks...")

                        found_in_fork = False
                        for fork_result in fork_candidates:
                            fork_info = fork_result['fork']
                            fork_owner = fork_info['owner']
                            fork_repo = fork_info['repo']

                            print(f"\n  \U0001f4e6 Checking fork: {fork_info['full_name']}")
                            print(f"     Score: {fork_result['score']}, Signals: {', '.join(fork_result['signals'])}")

                            fork_branches = self.github.get_branches(fork_owner, fork_repo)
                            fork_compatible = self.validator.pre_validate_branches(fork_owner, fork_repo, fork_branches)

                            fork_exact = [b for b in fork_compatible if b.minecraft_version == self.config.mc_version]

                            if fork_exact:
                                print(f"  \u2705 Found EXACT version {self.config.mc_version} in fork!")

                                for fb in fork_exact:
                                    is_clean, diff_bonus, diff_desc = self.validator.analyze_fork_diff(
                                        owner, repo, fork_owner, fork_repo, fb.name
                                    )
                                    fb.score += diff_bonus
                                    if is_clean or diff_bonus > 0:
                                        print(f"  \U0001f50d Diff analysis: {diff_desc}")

                                trust_score = fork_result.get('trust_score', 50)
                                trust_analysis = fork_info.get('trust_analysis', {})

                                print(f"\n  \u26a0\ufe0f  SECURITY NOTICE: Using community fork (not official)")
                                print(f"      Trust Score: {trust_score}% - ", end="")

                                if trust_score >= 80:
                                    print("HIGH confidence (established contributors)")
                                elif trust_score >= 60:
                                    print("MEDIUM confidence (some established contributors)")
                                elif trust_score >= 40:
                                    print("LOW confidence (new/unknown contributors)")
                                else:
                                    print("CRITICAL - Multiple red flags detected")

                                if trust_analysis.get('warnings'):
                                    print(f"      Warnings:")
                                    for warning in trust_analysis['warnings']:
                                        print(f"      - {warning}")

                                if trust_analysis.get('signals'):
                                    print(f"      Signals: {', '.join(trust_analysis['signals'])}")

                                print(f"      Review fork: {fork_info['url']}")
                                print(f"      Compiling code from: {fork_owner}")

                                owner = fork_owner
                                repo = fork_repo
                                compatible_branches = fork_exact
                                found_in_fork = True
                                break

                            fork_close = [b for b in fork_compatible if b.minecraft_version != self.config.mc_version]

                            if fork_close:
                                print(f"  \u2139\ufe0f  No exact match, checking if close matches support {self.config.mc_version}...")

                                best_branch = fork_close[0]
                                version_range = self.validator.parse_version_range_from_metadata(
                                    fork_owner, fork_repo, best_branch.name, self.config.loader
                                )

                                if version_range:
                                    if self.config.loader in ['neoforge', 'forge']:
                                        is_compat = is_version_in_maven_range(self.config.mc_version, version_range)
                                    else:
                                        is_compat = is_version_in_fabric_range(self.config.mc_version, version_range)

                                    if is_compat:
                                        print(f"  \u2705 Fork branch '{best_branch.name}' supports range {version_range}")
                                        print(f"     \u2192 Covers target {self.config.mc_version}!")

                                        for b in fork_close:
                                            b.version_range = version_range
                                            b.validation_method = 'fork_metadata_range'

                                        owner = fork_owner
                                        repo = fork_repo
                                        compatible_branches = fork_close
                                        found_in_fork = True
                                        print(f"  \u2705 Using fork with validated version range support!")
                                        break
                                    else:
                                        print(f"  \u274c Range {version_range} does not cover {self.config.mc_version}")
                                else:
                                    print(f"  \u26a0\ufe0f  Could not determine version range from metadata")

                            if fork_compatible and not close_matches:
                                owner = fork_owner
                                repo = fork_repo
                                compatible_branches = fork_compatible
                                found_in_fork = True
                                print(f"  \u2705 Found {len(compatible_branches)} compatible branches in fork!")
                                break

                        if not found_in_fork and close_matches:
                            print(f"\n  \u2139\ufe0f  No exact version in forks, using close matches from original repo")
                            compatible_branches = close_matches

                if not compatible_branches:
                    if (self.config.cross_loader
                            and self.config.loader in ("neoforge", "forge")
                            and self.modrinth.is_cross_loader_available()):
                        print(f"\n  \U0001f504 CROSS-LOADER: No NeoForge branches found, "
                              f"trying Fabric fallback via Sinytra Connector...")

                        fabric_all = self.github.get_branches(owner, repo)
                        fabric_branches = self.validator.pre_validate_branches(
                            owner, repo, fabric_all, override_loader="fabric"
                        )

                        if not fabric_branches and saved_fork_candidates:
                            for fork_result in saved_fork_candidates:
                                fi = fork_result['fork']
                                print(f"  \U0001f504 Checking fork {fi['full_name']} "
                                      f"for Fabric branches...")
                                fb = self.github.get_branches(fi['owner'], fi['repo'])
                                fabric_branches = self.validator.pre_validate_branches(
                                    fi['owner'], fi['repo'], fb,
                                    override_loader="fabric"
                                )
                                if fabric_branches:
                                    owner = fi['owner']
                                    repo = fi['repo']
                                    break

                        if fabric_branches:
                            print(f"  \u2705 Found {len(fabric_branches)} Fabric "
                                  f"branches for cross-loader compilation")
                            compatible_branches = fabric_branches
                            is_cross_loader_attempt = True
                        else:
                            return CompilationResult(
                                repo_url=repo_url,
                                success=False,
                                error=(f"No compatible branches in original repo "
                                       f"or forks for MC {self.config.mc_version}"
                                       f" + {self.config.loader} (also tried "
                                       f"Fabric cross-loader fallback)")
                            )
                    else:
                        return CompilationResult(
                            repo_url=repo_url,
                            success=False,
                            error=(f"No compatible branches in original repo "
                                   f"or forks for MC {self.config.mc_version}"
                                   f" + {self.config.loader}")
                        )

                print(f"  \U0001f3af Found {len(compatible_branches)} compatible branches")

                for branch in compatible_branches:
                    branch.score = self.validator.score_branch(branch)

                compatible_branches.sort(key=lambda b: b.score, reverse=True)

                print(f"\n  \U0001f4cb Top candidates:")
                for i, branch in enumerate(compatible_branches[:min(3, len(compatible_branches))], 1):
                    exact_indicator = "\u2713" if branch.minecraft_version == self.config.mc_version else "~"
                    days_old = ""
                    if branch.commit_date:
                        try:
                            commit_time = datetime.fromisoformat(branch.commit_date.replace('Z', '+00:00'))
                            now = datetime.now(timezone.utc)
                            days = (now - commit_time).days
                            if days < 30:
                                days_old = f", {days}d ago"
                            elif days < 365:
                                days_old = f", {days//30}mo ago"
                        except Exception:
                            pass
                    print(f"    {i}. {branch.name} (MC {branch.minecraft_version} {exact_indicator}, score: {branch.score}{days_old})")

                branches_to_try = compatible_branches

            # Try each branch
            branch_errors = []
            last_fail_type = FailureType.UNKNOWN
            last_missing_deps: List[str] = []
            last_fail_clone_dir: Optional[Path] = None
            for i, branch in enumerate(branches_to_try, 1):
                print(f"\n  \U0001f33f Attempting [{i}/{len(branches_to_try)}]: {branch.name}")
                version_match = "exact" if branch.minecraft_version == self.config.mc_version else "close"
                print(f"     MC: {branch.minecraft_version} ({version_match}), Loader: {branch.loader} {branch.loader_version}")

                if repo_temp_dir.exists():
                    shutil.rmtree(repo_temp_dir)

                clone_url = f"https://github.com/{owner}/{repo}.git"
                print(f"    \U0001f4e5 Cloning...")

                try:
                    result = subprocess.run(
                        ["git", "clone", "-b", branch.name, "--depth", "1", clone_url, str(repo_temp_dir)],
                        capture_output=True,
                        text=True,
                        timeout=300
                    )

                    if result.returncode != 0:
                        err = f"Clone failed: {result.stderr.strip()[:200]}"
                        print(f"    \u274c {err}")
                        branch_errors.append(f"{branch.name}: {err}")
                        last_fail_type = FailureType.CLONE_ERROR
                        continue

                except subprocess.TimeoutExpired:
                    branch_errors.append(f"{branch.name}: Clone timeout")
                    print(f"    \u274c Clone timeout")
                    last_fail_type = FailureType.CLONE_ERROR
                    continue
                except Exception as e:
                    branch_errors.append(f"{branch.name}: Clone error: {e}")
                    print(f"    \u274c Clone error: {e}")
                    last_fail_type = FailureType.CLONE_ERROR
                    continue

                print(f"    \U0001f50d Validating gradle.properties...")
                is_valid, message = self.validator.validate_gradle_properties(
                    repo_temp_dir,
                    skip_loader_validation=is_cross_loader_attempt
                )
                if not is_valid:
                    print(f"    \u274c {message}")
                    branch_errors.append(f"{branch.name}: {message}")
                    last_fail_type = FailureType.VALIDATION_ERROR
                    continue
                print(f"    {message}")

                print(f"    \U0001f50d Validating build.gradle...")
                is_valid, message = self.validator.validate_build_gradle(repo_temp_dir)
                if not is_valid:
                    print(f"    \u274c {message}")
                    branch_errors.append(f"{branch.name}: {message}")
                    last_fail_type = FailureType.VALIDATION_ERROR
                    continue
                print(f"    \u2705 {message}")

                success, jar_path, message, fail_type, missing_deps = \
                    compile_mod(repo_temp_dir, extra_gradle_args)
                if not success:
                    print(f"    \u274c {message}")
                    branch_errors.append(f"{branch.name}: {message[:200]}")
                    last_fail_type = fail_type
                    last_missing_deps = missing_deps
                    last_fail_clone_dir = repo_temp_dir
                    continue
                print(f"    \u2705 {message}")

                print(f"    \U0001f50d Validating JAR...")
                is_valid, mod_name, mod_version, message = validate_jar(
                    jar_path, self.config.mc_version
                )
                if not is_valid:
                    print(f"    \u274c {message}")
                    branch_errors.append(f"{branch.name}: JAR validation: {message}")
                    continue
                print(f"    \u2705 {message}")
                print(f"    \U0001f4cb Mod: {mod_name} v{mod_version}")

                dest_path = self.config.output_dir / jar_path.name
                shutil.copy2(jar_path, dest_path)
                print(f"    \U0001f4be Saved to: {dest_path}")

                if self.config.mods_path:
                    instance_dest = self.config.mods_path / jar_path.name
                    shutil.copy2(jar_path, instance_dest)
                    print(f"    \U0001f4be Installed to: {instance_dest}")

                version_note = ""
                if branch.minecraft_version != self.config.mc_version:
                    version_note = f" (compiled for MC {branch.minecraft_version})"
                cross_note = ""
                if is_cross_loader_attempt:
                    cross_note = " [Fabric via Sinytra Connector]"

                print(f"\n  \u2705 SUCCESS: {mod_name} v{mod_version} from branch '{branch.name}'{version_note}{cross_note}")

                return CompilationResult(
                    repo_url=repo_url,
                    success=True,
                    branch=branch.name,
                    jar_path=str(dest_path),
                    mod_name=mod_name,
                    mod_version=mod_version,
                    compiled_mc_version=branch.minecraft_version,
                    clone_dir=repo_temp_dir,
                    is_cross_loader=is_cross_loader_attempt
                )

            error_detail = f"All {len(branches_to_try)} branches failed:\n"
            for err in branch_errors:
                error_detail += f"  - {err}\n"
            return CompilationResult(
                repo_url=repo_url,
                success=False,
                error=error_detail.strip(),
                failure_type=last_fail_type,
                missing_dependencies=last_missing_deps,
                clone_dir=last_fail_clone_dir
            )

        except Exception as e:
            return CompilationResult(
                repo_url=repo_url,
                success=False,
                error=f"Unexpected error: {e}"
            )

    def process_repos(self, repo_urls: List[str]):
        """Process a list of repository URLs with dependency-aware multi-pass."""
        self.temp_dir = tempfile.mkdtemp(prefix="mod_compiler_")
        print(f"\U0001f5c2\ufe0f  Using temporary directory: {self.temp_dir}")

        try:
            print(f"\n{'='*80}")
            print(f"\U0001f4cb PASS 1: Compiling {len(repo_urls)} repositories")
            print(f"{'='*80}")

            pass1_results: Dict[str, CompilationResult] = {}
            for repo_url in repo_urls:
                try:
                    result = self.clone_and_compile(repo_url)
                except Exception as e:
                    logger.error(f"Unhandled error processing {repo_url}: {e}")
                    result = CompilationResult(
                        repo_url=repo_url,
                        success=False,
                        error=f"Unhandled error: {e}"
                    )
                pass1_results[repo_url] = result

                if result.success and result.clone_dir and result.clone_dir.exists():
                    publish_to_maven_local(result.clone_dir)

                time.sleep(1)

            dep_failures = [
                url for url, r in pass1_results.items()
                if not r.success
                and r.failure_type == FailureType.DEPENDENCY_RESOLUTION
            ]

            if dep_failures:
                modrinth_only = [
                    url for url, r in pass1_results.items()
                    if r.success and r.modrinth_download
                ]
                if modrinth_only:
                    print(f"\n{'='*80}")
                    print(f"\U0001f4e4 MAVEN PUBLISH: Compiling {len(modrinth_only)} "
                          f"Modrinth-downloaded mods for mavenLocal")
                    print(f"{'='*80}")

                    for repo_url in modrinth_only:
                        print(f"\n  \U0001f4e4 Compiling for mavenLocal: {repo_url}")
                        try:
                            compile_result = self.clone_and_compile(
                                repo_url, skip_modrinth=True
                            )
                            if (compile_result.success
                                    and compile_result.clone_dir
                                    and compile_result.clone_dir.exists()):
                                publish_to_maven_local(
                                    compile_result.clone_dir
                                )
                        except Exception as e:
                            print(f"    \u26a0\ufe0f  Maven publish failed: {e}")

                        time.sleep(1)

            if dep_failures:
                print(f"\n{'='*80}")
                print(f"\U0001f504 PASS 2: Retrying {len(dep_failures)} repos with "
                      f"dependency failures (mavenLocal injection)")
                print(f"{'='*80}")

                init_script = create_maven_local_init_script(self.temp_dir)
                maven_args = ["--init-script", str(init_script)]

                for repo_url in dep_failures:
                    prev = pass1_results[repo_url]
                    print(f"\n  \U0001f504 Retrying: {repo_url}")
                    if prev.missing_dependencies:
                        print(f"     Previously missing: "
                              f"{', '.join(prev.missing_dependencies)}")

                    try:
                        result = self.clone_and_compile(
                            repo_url, extra_gradle_args=maven_args
                        )
                    except Exception as e:
                        logger.error(
                            f"Unhandled error retrying {repo_url}: {e}"
                        )
                        result = CompilationResult(
                            repo_url=repo_url,
                            success=False,
                            error=f"Unhandled error (pass 2): {e}"
                        )

                    pass1_results[repo_url] = result

                    if result.success and result.clone_dir \
                            and result.clone_dir.exists():
                        publish_to_maven_local(
                            result.clone_dir, maven_args
                        )

                    time.sleep(1)

            self.results = list(pass1_results.values())

            cross_loader_mods = [
                r for r in self.results
                if r.success and r.is_cross_loader
            ]
            if cross_loader_mods:
                print(f"\n{'='*80}")
                print(f"\U0001f504 CROSS-LOADER: {len(cross_loader_mods)} Fabric mod(s) "
                      f"need Sinytra Connector to run on NeoForge")
                print(f"{'='*80}")
                print(f"  Downloading Sinytra Connector + Forgified Fabric API...")
                self.modrinth.download_modrinth_mod(
                    "connector", self.config.mc_version, "neoforge"
                )
                self.modrinth.download_modrinth_mod(
                    "forgified-fabric-api", self.config.mc_version, "neoforge"
                )

            if self.config.docker_test:
                self.docker.test_mods_in_docker(self.results)

        finally:
            print(f"\n\U0001f9f9 Cleaning up temporary directory...")
            if os.path.exists(self.temp_dir):
                safe_rmtree(Path(self.temp_dir))

    def generate_report(self) -> str:
        """Generate a detailed report of compilation results."""
        report_lines = []
        report_lines.append("\n" + "="*80)
        report_lines.append("\U0001f4ca COMPILATION REPORT")
        report_lines.append("="*80)

        successful = [r for r in self.results if r.success]
        failed = [r for r in self.results if not r.success]
        version_mismatches = [r for r in successful if r.compiled_mc_version and r.compiled_mc_version != self.config.mc_version]
        cross_loader_mods = [r for r in successful if r.is_cross_loader]

        report_lines.append(f"\n\u2705 Successful: {len(successful)}/{len(self.results)}")
        report_lines.append(f"\u274c Failed: {len(failed)}/{len(self.results)}")
        if version_mismatches:
            report_lines.append(f"\u26a0\ufe0f  Version warnings: {len(version_mismatches)}")
        if cross_loader_mods:
            report_lines.append(f"\U0001f504 Cross-loader (Fabric via Connector): {len(cross_loader_mods)}")

        if successful:
            report_lines.append("\n" + "-"*80)
            report_lines.append("\u2705 SUCCESSFULLY COMPILED MODS:")
            report_lines.append("-"*80)

            for result in successful:
                report_lines.append(f"\n\U0001f4e6 {result.repo_url}")
                report_lines.append(f"   \U0001f33f Branch: {result.branch}")
                report_lines.append(f"   \U0001f4cb Mod: {result.mod_name} v{result.mod_version}")

                if result.compiled_mc_version == self.config.mc_version:
                    report_lines.append(f"   \u2705 Version: {result.compiled_mc_version} (exact match)")
                else:
                    report_lines.append(f"   \u26a0\ufe0f  Version: {result.compiled_mc_version} (target was {self.config.mc_version})")

                if result.is_cross_loader:
                    report_lines.append(f"   \U0001f504 Fabric mod via Sinytra Connector")

                report_lines.append(f"   \U0001f4be JAR: {result.jar_path}")

        if version_mismatches:
            report_lines.append("\n" + "-"*80)
            report_lines.append("\u26a0\ufe0f  VERSION WARNINGS:")
            report_lines.append("-"*80)
            report_lines.append("Some mods were compiled for slightly different Minecraft versions.")
            report_lines.append("These will likely work, but TEST IN-GAME before using in production:")
            report_lines.append("")

            for result in version_mismatches:
                report_lines.append(f"  \u2022 {result.mod_name} v{result.mod_version}: Compiled for {result.compiled_mc_version} (you're using {self.config.mc_version})")

            report_lines.append("")
            report_lines.append("To require exact version matches, use --strict flag.")

        if failed:
            report_lines.append("\n" + "-"*80)
            report_lines.append("\u274c FAILED COMPILATIONS:")
            report_lines.append("-"*80)

            for result in failed:
                report_lines.append(f"\n\U0001f4e6 {result.repo_url}")
                report_lines.append(f"   \u274c Error: {result.error}")
                if result.failure_type == FailureType.DEPENDENCY_RESOLUTION:
                    report_lines.append(f"   \U0001f517 Type: Unresolved dependencies")
                    if result.missing_dependencies:
                        for dep in result.missing_dependencies:
                            report_lines.append(f"      - {dep}")

        if cross_loader_mods:
            report_lines.append("\n" + "-"*80)
            report_lines.append("\U0001f504 CROSS-LOADER MODS (Fabric via Sinytra Connector):")
            report_lines.append("-"*80)
            report_lines.append("These Fabric mods were compiled because no NeoForge version was found.")
            report_lines.append("They require Sinytra Connector + Forgified Fabric API to run on NeoForge.")
            report_lines.append("Compatibility is ~85% - some mods may have issues. TEST IN-GAME.")
            report_lines.append("")
            for result in cross_loader_mods:
                report_lines.append(f"  \u2022 {result.mod_name} v{result.mod_version} ({result.repo_url})")
            report_lines.append("")
            report_lines.append("Sinytra Connector: https://modrinth.com/mod/connector")
            report_lines.append("Forgified Fabric API: https://modrinth.com/mod/forgified-fabric-api")

        docker_tested = [r for r in self.results if r.docker_tested]
        if docker_tested:
            docker_passed = [r for r in docker_tested if r.docker_test_passed is True]
            docker_failed = [r for r in docker_tested if r.docker_test_passed is False]
            docker_inconclusive = [r for r in docker_tested if r.docker_test_passed is None]
            report_lines.append("\n" + "-"*80)
            report_lines.append("\U0001f433 DOCKER TEST RESULTS:")
            report_lines.append("-"*80)
            docker_client_only = [
                r for r in docker_inconclusive
                if r.docker_error and "[CLIENT-ONLY]" in r.docker_error
            ]
            docker_loader_err = [
                r for r in docker_inconclusive
                if r not in docker_client_only
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
                    "  \u26a0\ufe0f  LOADER/INFRASTRUCTURE ERROR "
                    "(not caused by mods):"
                )
                report_lines.append(
                    f"     {docker_loader_err[0].docker_error}"
                )
            if docker_failed:
                report_lines.append("")
                for r in docker_failed:
                    label = r.mod_name or r.repo_url
                    report_lines.append(f"  \u274c {label}: {r.docker_error}")

        report_lines.append("\n" + "="*80)
        report_lines.append(f"\U0001f3af Target: Minecraft {self.config.mc_version} with {self.config.loader.capitalize()} {self.config.loader_version}")
        if self.config.strict_version:
            report_lines.append(f"\U0001f512 Mode: STRICT (exact version matches only)")
        else:
            report_lines.append(f"\U0001f513 Mode: LENIENT (allows same major.minor versions)")
        report_lines.append(f"\U0001f4c1 Output: {self.config.output_dir}")
        if self.config.mods_path:
            report_lines.append(f"\U0001f4c1 Instance Mods: {self.config.mods_path}")
        report_lines.append("="*80)

        return '\n'.join(report_lines)
