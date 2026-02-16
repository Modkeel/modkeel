"""Data models for ModForge."""

import json
import hashlib
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional


class ModCompilerConfig:
    """Configuration for the mod compilation process."""

    def __init__(self, mc_version: str, loader: str, loader_version: str,
                 instance_path: Optional[str] = None, github_token: Optional[str] = None,
                 strict_version: bool = False, output_dir: str = "out",
                 cross_loader: bool = True, docker_test: bool = False,
                 docker_timeout: int = 180):
        self.mc_version = mc_version
        self.loader = loader.lower()
        self.loader_version = loader_version
        self.github_token = github_token
        self.strict_version = strict_version
        self.cross_loader = cross_loader
        self.docker_test = docker_test
        self.docker_timeout = docker_timeout

        # Output directory (always used)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Instance path (optional - if provided, JARs also go to instance/mods/)
        if instance_path:
            self.instance_path = Path(instance_path)
            if not self.instance_path.exists():
                raise ValueError(f"Instance path does not exist: {instance_path}")
            self.mods_path = self.instance_path / "mods"
            self.mods_path.mkdir(exist_ok=True)
        else:
            self.instance_path = None
            self.mods_path = None

        # GitHub API headers
        self.github_headers = {
            "Accept": "application/vnd.github.v3+json"
        }
        if github_token:
            self.github_headers["Authorization"] = f"token {github_token}"


class BranchCandidate:
    """Represents a potential branch for compilation."""

    def __init__(self, name: str, commit_sha: str, commit_date: str):
        self.name = name
        self.commit_sha = commit_sha
        self.commit_date = commit_date
        self.score = 0  # Will be calculated based on relevance

        # Pre-validation fields (populated via GitHub API)
        self.minecraft_version = None
        self.loader = None
        self.loader_version = None
        self.is_compatible = False
        self.validation_error = None

        # Metadata validation fields
        self.version_range = None  # e.g., "[1.21,1.22)" or "~1.21.0"
        self.validation_method = None  # 'metadata_range' or 'gradle_properties'

    def __repr__(self):
        return (f"BranchCandidate(name={self.name}, mc={self.minecraft_version}, "
                f"range={self.version_range}, loader={self.loader}, "
                f"compatible={self.is_compatible}, score={self.score})")


class FailureType(Enum):
    """Classification of build failures for dependency-aware retries."""
    NONE = "none"
    DEPENDENCY_RESOLUTION = "dependency_resolution"
    BUILD_ERROR = "build_error"
    CLONE_ERROR = "clone_error"
    VALIDATION_ERROR = "validation_error"
    TIMEOUT = "timeout"
    DOCKER_CRASH = "docker_crash"
    DOCKER_TIMEOUT = "docker_timeout"
    DOCKER_DEPENDENCY = "docker_dependency"
    UNKNOWN = "unknown"


class CompilationResult:
    """Result of attempting to compile a mod."""

    def __init__(self, repo_url: str, success: bool, branch: Optional[str] = None,
                 jar_path: Optional[str] = None, error: Optional[str] = None,
                 mod_name: Optional[str] = None, mod_version: Optional[str] = None,
                 compiled_mc_version: Optional[str] = None,
                 failure_type: "FailureType" = None,
                 missing_dependencies: Optional[List[str]] = None,
                 clone_dir: Optional[Path] = None,
                 is_cross_loader: bool = False,
                 modrinth_download: bool = False):
        self.repo_url = repo_url
        self.success = success
        self.branch = branch
        self.jar_path = jar_path
        self.error = error
        self.mod_name = mod_name
        self.mod_version = mod_version
        self.compiled_mc_version = compiled_mc_version
        self.failure_type = failure_type or FailureType.NONE
        self.missing_dependencies = missing_dependencies or []
        self.clone_dir = clone_dir
        self.is_cross_loader = is_cross_loader
        self.modrinth_download = modrinth_download
        # Docker test fields
        self.docker_tested: bool = False
        self.docker_test_passed: Optional[bool] = None
        self.docker_error: Optional[str] = None
        self.docker_load_time_ms: Optional[int] = None


class DockerTestCache:
    """Cache Docker test results to avoid re-testing identical JAR sets."""

    CACHE_DIR = Path.home() / ".modforge"
    CACHE_FILE = CACHE_DIR / "docker_test_cache.json"

    def __init__(self):
        self.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._data: Dict = self._load()

    def _load(self) -> Dict:
        if self.CACHE_FILE.exists():
            try:
                with open(self.CACHE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save(self) -> None:
        with open(self.CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)

    @staticmethod
    def compute_jar_set_hash(jar_paths: List[Path]) -> str:
        """Deterministic hash of a set of JAR files (sorted by content hash)."""
        file_hashes = []
        for jar in sorted(jar_paths):
            h = hashlib.sha256()
            with open(jar, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
            file_hashes.append(h.hexdigest())
        combined = hashlib.sha256(
            "|".join(sorted(file_hashes)).encode()
        )
        return combined.hexdigest()

    def get(self, jar_hash: str, mc_version: str,
            loader: str) -> Optional[bool]:
        """Return cached pass/fail or None if not cached / invalidated."""
        entry = self._data.get(jar_hash)
        if entry is None:
            return None
        if (entry.get("mc_version") != mc_version
                or entry.get("loader") != loader):
            return None
        return entry.get("passed")

    def set(self, jar_hash: str, passed: bool,
            mc_version: str, loader: str) -> None:
        """Store a Docker test result."""
        self._data[jar_hash] = {
            "passed": passed,
            "mc_version": mc_version,
            "loader": loader,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._save()
