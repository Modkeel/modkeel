#!/usr/bin/env python3
"""
Minecraft Mod Auto-Compiler
Automatically detects, compiles, and installs mods from GitHub repositories
for specific Minecraft versions and mod loaders.

Author: Juan - AutoKufe

NOTE: This file is a backwards-compatible shim. All logic has been moved
      to the modforge/ package. Each method delegates to the appropriate module.
      Direct usage: `python mod_auto_compiler.py ...` still works.
      Package usage: `modforge compile ...` (after pip install -e .)
"""

import argparse
import logging
import sys
import warnings
from pathlib import Path
from typing import Dict, List, Optional, Tuple

warnings.warn(
    "mod_auto_compiler.py is deprecated. Use 'modforge compile' instead. "
    "Install with: pip install -e . (from the ModForge directory)",
    DeprecationWarning,
    stacklevel=1,
)

# ============================================================================
# IMPORTS FROM modforge PACKAGE (re-exported for backwards compatibility)
# ============================================================================
from modforge.constants import (
    MODFORGE_VERSION, MODRINTH_USER_AGENT,
    MODFORGE_API_URL, MODFORGE_HMAC_KEY,
)
from modforge.models import (
    ModCompilerConfig, BranchCandidate, FailureType,
    CompilationResult, DockerTestCache,
)
from modforge.version import (
    compare_versions as _compare_versions_standalone,
    is_version_in_maven_range as _is_version_in_maven_range_standalone,
    is_version_in_fabric_range as _is_version_in_fabric_range_standalone,
    is_version_compatible as _is_version_compatible_standalone,
)
from modforge.config import ModForgeConfig, prompt_sharing_preference
from modforge.utils import (
    setup_logging, setup_windows_console, safe_rmtree,
)
from modforge.crowdsource import (
    sign_report, detect_java_version,
    build_report as _build_report_standalone,
    submit_reports as _submit_reports_standalone,
)
from modforge.github import (
    GitHubClient, parse_repo_url as _parse_repo_url_standalone,
)
from modforge.validation import BranchValidator
from modforge.build import (
    classify_build_failure as _classify_build_failure_standalone,
    compile_mod as _compile_mod_standalone,
    validate_jar as _validate_jar_standalone,
    create_maven_local_init_script as _create_maven_local_init_script_standalone,
    publish_to_maven_local as _publish_to_maven_local_standalone,
)
from modforge.modrinth import ModrinthClient
from modforge.docker import (
    DockerTester,
    DOCKER_SUCCESS_PATTERN as _DOCKER_SUCCESS_PATTERN,
    DOCKER_FAIL_PATTERNS as _DOCKER_FAIL_PATTERNS,
    DOCKER_LOADER_ERRORS as _DOCKER_LOADER_ERRORS,
    DOCKER_CLIENT_ONLY_PATTERN as _DOCKER_CLIENT_ONLY_PATTERN,
    DOCKER_DEP_PATTERN as _DOCKER_DEP_PATTERN,
    DOCKER_SERVER_STARTING_PATTERN as _DOCKER_SERVER_STARTING_PATTERN,
    DOCKER_VOLUME_PREFIX as _DOCKER_VOLUME_PREFIX,
    DOCKER_INSTALL_MAX_RETRIES as _DOCKER_INSTALL_MAX_RETRIES,
    NEOFORGE_VERSIONS as _NEOFORGE_VERSIONS,
)
from modforge.loaders import ALL_LOADERS
from modforge.pipeline import Pipeline

# ============================================================================
# LOGGING
# ============================================================================
logger = logging.getLogger("modforge")

try:
    import requests
except ImportError:
    print("Error: 'requests' library not found. Install it with: pip install requests")
    sys.exit(1)

try:
    import toml
except ImportError:
    print("Error: 'toml' library not found. Install it with: pip install toml")
    sys.exit(1)

setup_windows_console()
# ============================================================================


class ModAutoCompiler:
    """Main class for automatic mod compilation.

    NOTE: This class is a backwards-compatible shim. All logic has been
    moved to the modforge/ package. Each method delegates to the
    appropriate module.
    """

    # Docker constants re-exported for backward compatibility
    DOCKER_SUCCESS_PATTERN = _DOCKER_SUCCESS_PATTERN
    DOCKER_FAIL_PATTERNS = _DOCKER_FAIL_PATTERNS
    DOCKER_LOADER_ERRORS = _DOCKER_LOADER_ERRORS
    DOCKER_CLIENT_ONLY_PATTERN = _DOCKER_CLIENT_ONLY_PATTERN
    DOCKER_DEP_PATTERN = _DOCKER_DEP_PATTERN
    DOCKER_SERVER_STARTING_PATTERN = _DOCKER_SERVER_STARTING_PATTERN
    DOCKER_VOLUME_PREFIX = _DOCKER_VOLUME_PREFIX
    DOCKER_INSTALL_MAX_RETRIES = _DOCKER_INSTALL_MAX_RETRIES
    NEOFORGE_VERSIONS = _NEOFORGE_VERSIONS

    def __init__(self, config: ModCompilerConfig):
        self.config = config
        self.results: List[CompilationResult] = []
        self.temp_dir = None
        self._pipeline = Pipeline(config)

    # ── GitHub delegation ───────────────────────────────────────────────

    def parse_repo_url(self, url: str) -> Tuple[str, str, Optional[str]]:
        return _parse_repo_url_standalone(url)

    def get_repo_info(self, owner: str, repo: str) -> Optional[Dict]:
        return self._pipeline.github.get_repo_info(owner, repo)

    def search_compatible_repos(
        self, original_owner: str, original_repo: str
    ) -> List[Dict]:
        cross = (
            self._pipeline.modrinth.is_cross_loader_available()
            if self.config.cross_loader
            and self.config.loader in ("neoforge", "forge")
            else False
        )
        return self._pipeline.github.search_compatible_repos(
            original_owner, original_repo, cross
        )

    def analyze_contributor_trust(self, full_repo_name: str) -> Dict:
        return self._pipeline.github.analyze_contributor_trust(full_repo_name)

    def get_user_details(self, username: str) -> Optional[Dict]:
        return self._pipeline.github.get_user_details(username)

    def get_contributor_count(self, full_repo_name: str) -> int:
        return self._pipeline.github.get_contributor_count(full_repo_name)

    def get_commit_count(self, full_repo_name: str) -> int:
        return self._pipeline.github.get_commit_count(full_repo_name)

    def score_fork_reliability(self, fork_data: Dict) -> Dict:
        return self._pipeline.github.score_fork_reliability(fork_data)

    def get_branches(
        self, owner: str, repo: str
    ) -> List[BranchCandidate]:
        return self._pipeline.github.get_branches(owner, repo)

    def get_file_from_repo(
        self, owner: str, repo: str, branch: str, filepath: str
    ) -> Optional[str]:
        return self._pipeline.github.get_file_from_repo(
            owner, repo, branch, filepath
        )

    # ── Validation delegation ───────────────────────────────────────────

    def filter_branches_by_version_proximity(
        self, branches: List[BranchCandidate]
    ) -> List[BranchCandidate]:
        return self._pipeline.validator.filter_branches_by_version_proximity(
            branches
        )

    def parse_version_range_from_metadata(
        self, owner: str, repo: str, branch_name: str, loader: str
    ) -> Optional[str]:
        return self._pipeline.validator.parse_version_range_from_metadata(
            owner, repo, branch_name, loader
        )

    def pre_validate_branch(
        self, owner: str, repo: str, branch: BranchCandidate,
        override_loader: Optional[str] = None
    ) -> bool:
        return self._pipeline.validator.pre_validate_branch(
            owner, repo, branch, override_loader
        )

    def is_version_compatible(
        self, found_version: str, target_version: str
    ) -> bool:
        return _is_version_compatible_standalone(
            found_version, target_version, strict=self.config.strict_version
        )

    def pre_validate_branches(
        self, owner: str, repo: str,
        branches: List[BranchCandidate],
        override_loader: Optional[str] = None
    ) -> List[BranchCandidate]:
        return self._pipeline.validator.pre_validate_branches(
            owner, repo, branches, override_loader
        )

    def score_branch(self, branch: BranchCandidate) -> int:
        return self._pipeline.validator.score_branch(branch)

    def analyze_fork_diff(
        self, original_owner: str, original_repo: str,
        fork_owner: str, fork_repo: str,
        branch: str
    ) -> Tuple[bool, int, str]:
        return self._pipeline.validator.analyze_fork_diff(
            original_owner, original_repo, fork_owner, fork_repo, branch
        )

    def validate_gradle_properties(
        self, repo_path: Path, skip_loader_validation: bool = False
    ) -> Tuple[bool, str]:
        return self._pipeline.validator.validate_gradle_properties(
            repo_path, skip_loader_validation
        )

    def validate_build_gradle(self, repo_path: Path) -> Tuple[bool, str]:
        return self._pipeline.validator.validate_build_gradle(repo_path)

    # ── Version delegation ──────────────────────────────────────────────

    def is_version_in_maven_range(
        self, version: str, range_str: str
    ) -> bool:
        return _is_version_in_maven_range_standalone(version, range_str)

    def is_version_in_fabric_range(
        self, version: str, range_str: str
    ) -> bool:
        return _is_version_in_fabric_range_standalone(version, range_str)

    def _compare_versions(self, v1: str, v2: str) -> int:
        return _compare_versions_standalone(v1, v2)

    # ── Build delegation ────────────────────────────────────────────────

    def classify_build_failure(
        self, stderr: str, stdout: str
    ) -> Tuple[FailureType, List[str]]:
        return _classify_build_failure_standalone(stderr, stdout)

    def compile_mod(
        self, repo_path: Path,
        extra_gradle_args: Optional[List[str]] = None
    ) -> Tuple[bool, Optional[Path], str, FailureType, List[str]]:
        return _compile_mod_standalone(repo_path, extra_gradle_args)

    def validate_jar(
        self, jar_path: Path
    ) -> Tuple[bool, Optional[str], Optional[str], str]:
        return _validate_jar_standalone(jar_path, self.config.mc_version)

    def create_maven_local_init_script(self) -> Path:
        return _create_maven_local_init_script_standalone(self.temp_dir)

    def publish_to_maven_local(
        self, repo_path: Path,
        extra_gradle_args: Optional[List[str]] = None
    ) -> bool:
        return _publish_to_maven_local_standalone(repo_path, extra_gradle_args)

    # ── Modrinth delegation ─────────────────────────────────────────────

    def is_cross_loader_available(self) -> bool:
        return self._pipeline.modrinth.is_cross_loader_available()

    def check_modrinth(self, mod_name: str) -> Optional[Dict]:
        return self._pipeline.modrinth.check_modrinth(mod_name)

    def _download_modrinth_deps(
        self, modrinth_result: Dict, _seen: Optional[set] = None,
    ) -> None:
        self._pipeline.modrinth.download_modrinth_deps(
            modrinth_result, _seen
        )

    def download_modrinth_mod(
        self, slug: str, mc_version: str, loader: str
    ) -> Optional[Path]:
        return self._pipeline.modrinth.download_modrinth_mod(
            slug, mc_version, loader
        )

    # ── Docker delegation ───────────────────────────────────────────────

    def check_docker_available(self) -> bool:
        return self._pipeline.docker.check_docker_available()

    def _kill_container(self, name: str) -> None:
        self._pipeline.docker._kill_container(name)

    def _get_docker_volume_name(self) -> str:
        return self._pipeline.docker._get_docker_volume_name()

    def _ensure_docker_volume(self) -> str:
        return self._pipeline.docker._ensure_docker_volume()

    def _get_loader_cache_dir(self) -> Path:
        return self._pipeline.docker._get_loader_cache_dir()

    def _is_loader_installed(self, cache_dir: Path) -> bool:
        return self._pipeline.docker._is_loader_installed(cache_dir)

    def _download_installer_with_resume(
        self, url: str, dest: Path
    ) -> bool:
        return self._pipeline.docker._download_installer_with_resume(
            url, dest
        )

    def _ensure_loader_installed(self) -> Optional[Path]:
        return self._pipeline.docker._ensure_loader_installed()

    def _run_docker_server(self, mods_dir: Path) -> dict:
        return self._pipeline.docker._run_docker_server(mods_dir)

    def _run_docker_server_direct(
        self, mods_dir: Path, loader_dir: Path
    ) -> dict:
        return self._pipeline.docker._run_docker_server_direct(
            mods_dir, loader_dir
        )

    def _run_docker_server_fallback(
        self, mods_dir: Path, loader_type: str
    ) -> dict:
        return self._pipeline.docker._run_docker_server_fallback(
            mods_dir, loader_type
        )

    def _analyze_server_logs(self, process) -> dict:
        return self._pipeline.docker._analyze_server_logs(process)

    @staticmethod
    def _extract_missing_deps_from_logs(
        log_lines: List[str],
    ) -> List[str]:
        return DockerTester._extract_missing_deps_from_logs(log_lines)

    def _collect_dep_jars(
        self, exclude: Optional[set] = None
    ) -> List[Path]:
        return self._pipeline.docker._collect_dep_jars(
            self.results, exclude
        )

    def _test_batch_docker(self, jar_paths: List[Path]) -> dict:
        return self._pipeline.docker._test_batch_docker(
            jar_paths, self.results
        )

    def _test_single_docker(self, jar_path: Path) -> dict:
        return self._pipeline.docker._test_single_docker(
            jar_path, self.results
        )

    def test_mods_in_docker(self) -> None:
        self._pipeline.docker.test_mods_in_docker(self.results)

    # ── Pipeline delegation ─────────────────────────────────────────────

    def clone_and_compile(
        self, repo_url: str, specific_branch: Optional[str] = None,
        extra_gradle_args: Optional[List[str]] = None,
        skip_modrinth: bool = False
    ) -> CompilationResult:
        return self._pipeline.clone_and_compile(
            repo_url, specific_branch, extra_gradle_args, skip_modrinth
        )

    def process_repos(self, repo_urls: List[str]):
        self._pipeline.process_repos(repo_urls)
        self.results = self._pipeline.results

    def _safe_rmtree(self, path):
        safe_rmtree(path)

    # ── Crowdsource delegation ──────────────────────────────────────────

    @staticmethod
    def _detect_java_version() -> str:
        return detect_java_version()

    def build_report(self, result: CompilationResult) -> Optional[Dict]:
        return _build_report_standalone(
            result, self.config.mc_version, self.config.loader,
            self.config.loader_version, self.parse_repo_url,
        )

    def submit_reports(self, modforge_config: "ModForgeConfig") -> None:
        _submit_reports_standalone(
            self.results, modforge_config, self.config.mc_version,
            self.config.loader, self.config.loader_version,
            self.parse_repo_url,
        )

    def generate_report(self) -> str:
        self._pipeline.results = self.results
        return self._pipeline.generate_report()


def main():
    parser = argparse.ArgumentParser(
        description="Automatically compile Minecraft mods from GitHub repositories",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Compile to out/ directory (default)
  %(prog)s --mc-version 1.21.10 --loader neoforge --loader-version 64 repos.txt

  # Compile and install to instance
  %(prog)s --mc-version 1.21.10 --loader neoforge --loader-version 64 --instance "~/.minecraft/instances/MyInstance" repos.txt

  # Custom output directory
  %(prog)s --mc-version 1.21.10 --loader neoforge --loader-version 64 --output-dir build/mods repos.txt
        """
    )

    parser.add_argument(
        'repos_file',
        help='Text file containing GitHub repository URLs (one per line)'
    )

    parser.add_argument(
        '--mc-version',
        required=True,
        help='Minecraft version (e.g., 1.21.10)'
    )

    parser.add_argument(
        '--loader',
        required=True,
        choices=ALL_LOADERS,
        help='Mod loader type'
    )

    parser.add_argument(
        '--loader-version',
        required=True,
        help='Mod loader version (e.g., 64 for NeoForge)'
    )

    parser.add_argument(
        '--instance',
        help='Path to Minecraft instance directory (optional, JARs also copied here)'
    )

    parser.add_argument(
        '--output-dir',
        default='out',
        help='Directory for compiled JARs (default: out/)'
    )

    parser.add_argument(
        '--github-token',
        help='GitHub Personal Access Token (recommended to avoid rate limits)'
    )

    parser.add_argument(
        '--strict',
        action='store_true',
        help='Require exact Minecraft version match (default: lenient mode allows same major.minor versions)'
    )

    parser.add_argument(
        '--no-cross-loader',
        action='store_true',
        help='Disable Fabric fallback via Sinytra Connector for NeoForge'
    )

    parser.add_argument(
        '--output-report',
        help='Path to save the compilation report (optional)'
    )

    parser.add_argument(
        '--log-file',
        help='Path to write a log file (optional, in addition to stdout)'
    )

    parser.add_argument(
        '--docker-test',
        action='store_true',
        help='Test compiled mods in a headless Docker Minecraft server'
    )

    parser.add_argument(
        '--docker-timeout',
        type=int,
        default=180,
        help='Seconds to wait for Docker server startup (default: 180)'
    )

    parser.add_argument(
        '--no-share',
        action='store_true',
        help='Skip anonymous data sharing for this run'
    )

    args = parser.parse_args()

    # Setup logging
    setup_logging(args.log_file)

    # Read repository URLs
    repos_file = Path(args.repos_file)
    if not repos_file.exists():
        print(f"\u274c Error: Repository file not found: {args.repos_file}")
        sys.exit(1)

    with open(repos_file, 'r', encoding='utf-8') as f:
        repo_urls = [
            line.strip() for line in f
            if line.strip() and not line.startswith('#')
        ]

    if not repo_urls:
        print(f"\u274c Error: No repository URLs found in {args.repos_file}")
        sys.exit(1)

    print(f"\U0001f4cb Loaded {len(repo_urls)} repositories from {args.repos_file}")

    # Load persistent ModForge config (sharing preferences, client_id)
    modforge_cfg = ModForgeConfig()

    # First-run: ask about anonymous data sharing
    if modforge_cfg.is_first_run and not modforge_cfg.was_prompted:
        prompt_sharing_preference(modforge_cfg)

    # Override sharing if --no-share flag is set
    no_share = args.no_share

    # Create configuration
    try:
        config = ModCompilerConfig(
            mc_version=args.mc_version,
            loader=args.loader,
            loader_version=args.loader_version,
            instance_path=args.instance,
            github_token=args.github_token,
            strict_version=args.strict,
            output_dir=args.output_dir,
            cross_loader=not args.no_cross_loader,
            docker_test=args.docker_test,
            docker_timeout=args.docker_timeout
        )
    except ValueError as e:
        print(f"\u274c Configuration error: {e}")
        sys.exit(1)

    # Create compiler and process
    compiler = ModAutoCompiler(config)
    compiler.process_repos(repo_urls)

    # Generate and display report
    report = compiler.generate_report()
    print(report)

    # Save report if requested
    if args.output_report:
        with open(args.output_report, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"\n\U0001f4be Report saved to: {args.output_report}")

    # Submit anonymous crowdsource reports (last step, silent failure)
    if not no_share:
        try:
            compiler.submit_reports(modforge_cfg)
        except Exception as e:
            logger.debug("Crowdsource submission error: %s", e)


if __name__ == "__main__":
    main()
