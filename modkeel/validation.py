"""Branch validation and scoring for Modkeel."""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from modkeel.buildinfo import BuildInfo, extract_build_info, select_build_files
from modkeel.loaders import get_profile
from modkeel.models import BranchCandidate, ModCompilerConfig
from modkeel.version import (
    is_version_compatible,
    is_version_in_fabric_range,
    is_version_in_maven_range,
)

logger = logging.getLogger("modkeel")

NON_CODE_BRANCH = re.compile(
    r'(?:^|[/_\-])(?:l10n|i18n|crowdin|translations?|dependabot|renovate|gh-pages|docs?|'
    r'vitepress-docs)(?:$|[/_\-])',
    re.IGNORECASE,
)


def is_bounded_range(version_range: str) -> bool:
    """True if a Minecraft range has an upper bound (an open range proves nothing)."""
    r = version_range.strip()
    if r.startswith(("[", "(")):
        inner = r[1:-1]
        return "," not in inner or bool(inner.split(",", 1)[1].strip())
    if r.startswith((">", "*")):
        return "<" in r
    return True


class BranchValidator:
    """Validates and scores branches for compatibility."""

    def __init__(self, github_client, config: ModCompilerConfig):
        self.github = github_client
        self.config = config

    def filter_branches_by_version_proximity(
        self, branches: List[BranchCandidate]
    ) -> List[BranchCandidate]:
        """Filter branches to only keep those close to target version."""
        target_parts = self.config.mc_version.split('.')
        target_major_minor = '.'.join(target_parts[:2])

        filtered = []

        for branch in branches:
            version_match = re.search(
                r'(?:^|[^.\d])(1\.\d+(?:\.\d+)?)(?:[^.\d]|$)', branch.name
            )

            if version_match:
                branch_version = version_match.group(1)
                branch_parts = branch_version.split('.')
                branch_major_minor = '.'.join(branch_parts[:2])

                if branch_major_minor == target_major_minor:
                    filtered.append(branch)
            else:
                filtered.append(branch)

        if len(filtered) < len(branches):
            removed = len(branches) - len(filtered)
            print(f"  \u2139\ufe0f  Filtered {removed} branches from other MC versions (keeping {target_major_minor}.x only)")

        return filtered

    def parse_version_range_from_metadata(
        self, owner: str, repo: str, branch_name: str, loader: str
    ) -> Optional[str]:
        """Parse version range from mod metadata files."""
        profile = get_profile(loader)
        locations = profile["source_metadata_locations"]
        fmt = profile["source_metadata_format"]

        for location in locations:
            content = self.github.get_file_from_repo(owner, repo, branch_name, location)
            if not content:
                continue

            if fmt == "toml":
                pattern = r'\[\[dependencies\.[^\]]+\]\].*?modId\s*=\s*["\']minecraft["\'].*?versionRange\s*=\s*["\']([^"\']+)["\']'
                match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)
                if match:
                    return match.group(1)

            elif fmt == "json":
                try:
                    data = json.loads(content)
                    # Quilt schema: quilt_loader.depends[{id:"minecraft", versions:"..."}]
                    quilt_loader = data.get('quilt_loader', {})
                    if quilt_loader:
                        for dep in quilt_loader.get('depends', []):
                            if isinstance(dep, dict) and dep.get('id') == 'minecraft':
                                versions = dep.get('versions')
                                if versions:
                                    return versions if isinstance(versions, str) else str(versions)
                    # Fabric schema: depends.minecraft
                    mc_dep = data.get('depends', {}).get('minecraft', '')
                    if mc_dep:
                        return mc_dep
                except Exception:
                    pass

        return None

    def pre_validate_branch(
        self, owner: str, repo: str, branch: BranchCandidate,
        override_loader: Optional[str] = None
    ) -> bool:
        """
        Pre-validate a branch from its build files, without cloning.
        Updates branch object with validation results.
        """
        target_loader = override_loader or self.config.loader

        if NON_CODE_BRANCH.search(branch.name):
            branch.validation_error = "Not a code branch (translations/docs/bots)"
            return False

        paths = self.github.get_tree(owner, repo, branch.name)
        if paths is None:
            return self._pre_validate_by_fixed_paths(owner, repo, branch, target_loader)

        wanted = select_build_files(paths)
        files = {}
        for path in wanted:
            content = self.github.get_raw_file(owner, repo, branch.name, path)
            if content is not None:
                files[path] = content
        info = extract_build_info(paths, files)
        return self.judge_build_info(branch, info, target_loader)

    def judge_build_info(
        self, branch: BranchCandidate, info: BuildInfo, target_loader: str
    ) -> bool:
        """Decide compatibility from what the branch declares. Updates branch in place."""
        mc = self.config.mc_version
        branch.build_info = info

        if target_loader in info.loaders:
            branch.loader = target_loader
        elif info.loaders:
            branch.loader = sorted(info.loaders)[0]
            others = ", ".join(sorted(info.loaders))
            if target_loader in ("neoforge", "forge") and info.loaders <= {"fabric", "quilt"}:
                branch.validation_error = f"Fabric-only mod (no {target_loader} version)"
            else:
                branch.validation_error = f"Loader mismatch: {others} != {target_loader}"
            return False

        ranges = info.ranges_for(target_loader) or [
            r for rs in info.ranges.values() for r in rs]
        range_format = get_profile(target_loader)["version_range_format"]

        def covers(version_range: str) -> bool:
            if range_format == "maven" and version_range.lstrip().startswith(("[", "(")):
                return is_version_in_maven_range(mc, version_range)
            return is_version_in_fabric_range(mc, version_range)

        if info.targets and any(v == mc for v, _ in info.targets)                 and (mc, target_loader) not in info.targets:
            others = ", ".join(sorted(ldr for v, ldr in info.targets if v == mc))
            branch.validation_error = f"MC {mc} is built only for {others}, not {target_loader}"
            return False

        if mc in info.mc_versions:
            branch.minecraft_version = mc
            branch.validation_method = 'build_target'
        elif info.mc_versions:
            same_family = sorted(
                v for v in info.mc_versions
                if is_version_compatible(v, mc, strict=self.config.strict_version))
            if not same_family:
                found = ", ".join(sorted(info.mc_versions))
                branch.validation_error = f"MC version mismatch: {found} != {mc}"
                return False
            branch.minecraft_version = same_family[-1]
            bounded = [r for r in ranges if is_bounded_range(r)]
            covering = [r for r in bounded if covers(r)]
            if covering:
                branch.version_range = covering[0]
                branch.validation_method = 'metadata_range'
            elif bounded:
                branch.version_range = bounded[0]
                branch.validation_error = f"MC {mc} not in declared range {bounded[0]}"
                return False
            else:
                branch.validation_method = 'build_target'
        elif ranges and not any(covers(r) for r in ranges):
            branch.version_range = ranges[0]
            branch.validation_error = f"MC {mc} not in range {ranges[0]}"
            return False
        elif any(covers(r) and is_bounded_range(r) for r in ranges):
            # An open range such as [1.21.3,) covers everything later and proves nothing
            branch.version_range = next(r for r in ranges if covers(r) and is_bounded_range(r))
            branch.validation_method = 'metadata_range'
        elif re.search(r'(?:^|[^.\d])' + re.escape(mc) + r'(?:[^.\d]|$)', branch.name):
            branch.validation_method = 'branch_name'
        else:
            branch.validation_error = 'Undetermined: no Minecraft version declared in build files'
            return False

        if not branch.loader:
            branch.validation_error = 'Undetermined: no loader declared in build files'
            return False

        branch.is_compatible = True
        return True

    def _pre_validate_by_fixed_paths(
        self, owner: str, repo: str, branch: BranchCandidate, target_loader: str
    ) -> bool:
        """Fallback when the tree listing is unavailable: probe well-known paths."""

        # STEP 1: Extract MC version and loader from gradle.properties
        gradle_content = self.github.get_file_from_repo(owner, repo, branch.name, 'gradle.properties')

        if gradle_content:
            mc_match = re.search(r'minecraft_version\s*=\s*["\']?([0-9.]+)["\']?', gradle_content)
            if not mc_match:
                mc_match = re.search(r'mc_version\s*=\s*["\']?([0-9.]+)["\']?', gradle_content)

            if mc_match:
                branch.minecraft_version = mc_match.group(1)

            # Try to match target loader version patterns
            target_profile = get_profile(target_loader)
            for pattern in target_profile["gradle_version_patterns"]:
                loader_match = re.search(pattern, gradle_content)
                if loader_match:
                    branch.loader = target_loader
                    branch.loader_version = loader_match.group(1)
                    break

            # If target not found, detect any loader via all profiles
            if not branch.loader:
                from modkeel.loaders import LOADER_PROFILES
                for ldr_name, ldr_profile in LOADER_PROFILES.items():
                    for pattern in ldr_profile["gradle_detection_patterns"]:
                        if re.search(pattern, gradle_content):
                            branch.loader = ldr_name
                            break
                    if branch.loader:
                        break

            if branch.loader and branch.loader != target_loader:
                branch.validation_error = f"Wrong loader: found {branch.loader}, need {target_loader}"
                return False

        # STEP 1b: Try libs.versions.toml
        if not branch.minecraft_version:
            libs_versions = self.github.get_file_from_repo(
                owner, repo, branch.name, 'gradle/libs.versions.toml')
            if libs_versions:
                try:
                    import toml as toml_parser
                    versions_data = toml_parser.loads(libs_versions)

                    if 'versions' in versions_data:
                        versions = versions_data['versions']
                        mc_version = (versions.get('minecraft')
                                      or versions.get('minecraft-version')
                                      or versions.get('game-version'))

                        if mc_version:
                            if isinstance(mc_version, dict):
                                mc_version = mc_version.get('ref') or mc_version.get('version')
                            branch.minecraft_version = str(mc_version).strip('"')

                        if not branch.loader:
                            if 'neoforge' in str(versions_data).lower():
                                branch.loader = 'neoforge'
                                branch.loader_version = str(
                                    versions.get('neoforge', 'unknown'))
                            elif 'fabric' in str(versions_data).lower():
                                branch.loader = 'fabric'
                except Exception:
                    pass

        # STEP 1c: Try fabric.mod.json
        if not branch.minecraft_version or not branch.loader:
            fabric_json = self.github.get_file_from_repo(
                owner, repo, branch.name, 'src/main/resources/fabric.mod.json')
            if fabric_json:
                try:
                    data = json.loads(fabric_json)

                    depends = data.get('depends', {})
                    mc_dep = depends.get('minecraft', '')

                    if mc_dep:
                        mc_version_match = re.search(r'(\d+\.\d+(?:\.\d+)?)', mc_dep)
                        if mc_version_match and not branch.minecraft_version:
                            branch.minecraft_version = mc_version_match.group(1)

                        if not branch.loader:
                            branch.loader = 'fabric'
                            loader_ver = depends.get(
                                'fabricloader', depends.get('fabric-loader', ''))
                            if loader_ver:
                                loader_match = re.search(
                                    r'(\d+\.\d+(?:\.\d+)?)', str(loader_ver))
                                if loader_match:
                                    branch.loader_version = loader_match.group(1)
                except Exception:
                    pass

        # STEP 2: Read metadata files for AUTHORITATIVE version range validation
        version_range = self.parse_version_range_from_metadata(owner, repo, branch.name, target_loader)

        if version_range and "${" not in version_range:
            branch.version_range = version_range

            if branch.loader and branch.loader != target_loader:
                branch.validation_error = f"Loader mismatch: {branch.loader} != {target_loader}"
                return False

            range_format = get_profile(target_loader)["version_range_format"]
            if range_format == "maven":
                is_compat = is_version_in_maven_range(self.config.mc_version, version_range)
            else:
                is_compat = is_version_in_fabric_range(self.config.mc_version, version_range)

            if is_compat:
                branch.is_compatible = True
                branch.validation_method = 'metadata_range'
                return True
            else:
                branch.validation_error = f"MC {self.config.mc_version} not in range {version_range}"
                return False

        # STEP 3: Fallback to gradle.properties exact/lenient matching
        if not branch.minecraft_version:
            branch.validation_error = 'minecraft_version not found in gradle.properties or version catalog'
            return False

        if not is_version_compatible(branch.minecraft_version, self.config.mc_version,
                                     strict=self.config.strict_version):
            branch.validation_error = f"MC version mismatch: {branch.minecraft_version} != {self.config.mc_version}"
            return False

        if branch.loader != target_loader:
            if branch.loader == 'fabric' and target_loader in ['neoforge', 'forge']:
                branch.validation_error = f"Fabric-only mod (no {target_loader} version)"
            elif branch.loader in ['neoforge', 'forge'] and target_loader == 'fabric':
                branch.validation_error = f"{branch.loader.capitalize()}-only mod (no Fabric version)"
            else:
                branch.validation_error = f"Loader mismatch: {branch.loader or 'unknown'} != {target_loader}"
            return False

        branch.is_compatible = True
        branch.validation_method = 'gradle_properties'
        return True

    def pre_validate_branches(
        self, owner: str, repo: str,
        branches: List[BranchCandidate],
        override_loader: Optional[str] = None
    ) -> List[BranchCandidate]:
        """Pre-validate multiple branches using GitHub API."""
        loader_label = override_loader or self.config.loader
        print(f"  \U0001f50d Pre-validating {len(branches)} branches via GitHub API"
              f" (loader={loader_label})...")

        compatible_branches = []
        if not branches:
            return compatible_branches
        max_workers = min(10, len(branches))

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_branch = {
                executor.submit(self.pre_validate_branch, owner, repo, branch,
                                override_loader): branch
                for branch in branches
            }

            for future in as_completed(future_to_branch):
                branch = future_to_branch[future]
                try:
                    is_compatible_result = future.result()

                    if is_compatible_result:
                        if branch.validation_method == 'metadata_range':
                            print(f"    \u2705 {branch.name}: Range {branch.version_range} \u2192 covers MC {self.config.mc_version}")
                        else:
                            version_indicator = "\u2713" if branch.minecraft_version == self.config.mc_version else "~"
                            print(f"    \u2705 {branch.name}: MC {branch.minecraft_version} {version_indicator} + {branch.loader}")

                        compatible_branches.append(branch)
                    else:
                        print(f"    \u274c {branch.name}: {branch.validation_error}")

                except Exception as e:
                    print(f"    \u274c {branch.name}: Validation error - {e}")

        return compatible_branches

    def score_branch(self, branch: BranchCandidate) -> int:
        """Score a branch that has already been pre-validated as compatible."""
        score = 0

        if branch.minecraft_version == self.config.mc_version:
            score += 1000
        else:
            score += 500

        if branch.commit_date:
            try:
                commit_time = datetime.fromisoformat(branch.commit_date.replace('Z', '+00:00'))
                now = datetime.now(timezone.utc)
                days_old = (now - commit_time).days

                if days_old < 30:
                    score += 300
                elif days_old < 90:
                    score += 200
                elif days_old < 180:
                    score += 100
                else:
                    score -= 100
            except Exception:
                pass

        name_lower = branch.name.lower()

        if name_lower in ['main', 'master']:
            score += 150

        if re.search(r'(?:^|[/\-_])dev(?:$|[/\-_])', name_lower):
            score += 100

        return score

    def analyze_fork_diff(
        self, original_owner: str, original_repo: str,
        fork_owner: str, fork_repo: str,
        branch: str
    ) -> Tuple[bool, int, str]:
        """Compare a fork branch against the original repo's default branch."""
        VERSION_FILES = {
            'gradle.properties', 'build.gradle', 'build.gradle.kts',
            'settings.gradle', 'settings.gradle.kts',
            'gradle/libs.versions.toml', 'gradle/wrapper/gradle-wrapper.properties',
        }
        METADATA_FILES = {
            'src/main/resources/META-INF/mods.toml',
            'src/main/resources/META-INF/neoforge.mods.toml',
            'src/main/resources/fabric.mod.json',
            'src/main/resources/quilt.mod.json',
        }
        SAFE_FILES = VERSION_FILES | METADATA_FILES

        compare_url = (
            f"https://api.github.com/repos/{original_owner}/{original_repo}"
            f"/compare/HEAD...{fork_owner}:{branch}"
        )

        try:
            response = requests.get(
                compare_url, headers=self.config.github_headers, timeout=10
            )
            if response.status_code != 200:
                return False, 0, f"Compare API returned {response.status_code}"

            data = response.json()
            files = data.get('files', [])
            total_files = len(files)

            if total_files == 0:
                return False, 0, "No file differences found"

            safe_changes = []
            source_changes = []
            for f in files:
                filename = f.get('filename', '')
                if filename in SAFE_FILES or any(filename.endswith(s) for s in (
                    'gradle.properties', 'build.gradle', 'build.gradle.kts',
                    'mods.toml', 'neoforge.mods.toml', 'fabric.mod.json',
                )):
                    safe_changes.append(filename)
                else:
                    source_changes.append(filename)

            safe_count = len(safe_changes)
            source_count = len(source_changes)

            if source_count == 0:
                return True, 200, (
                    f"Clean port: {safe_count} build/version files changed, "
                    f"0 source files changed"
                )
            elif source_count <= 3 and safe_count > 0:
                return True, 100, (
                    f"Mostly clean port: {safe_count} build files + "
                    f"{source_count} source files changed"
                )
            elif source_count <= 10:
                return False, 50, (
                    f"Moderate changes: {safe_count} build files + "
                    f"{source_count} source files changed"
                )
            else:
                return False, 0, (
                    f"Extensive changes: {source_count} source files changed"
                )

        except requests.RequestException as e:
            return False, 0, f"Compare API error: {e}"
        except (KeyError, ValueError):
            return False, 0, "Failed to parse compare response"

    def validate_gradle_properties(
        self, repo_path: Path,
        skip_loader_validation: bool = False
    ) -> Tuple[bool, str]:
        """Validate gradle.properties file for version compatibility."""
        gradle_props = repo_path / "gradle.properties"

        if not gradle_props.exists():
            return False, "gradle.properties not found"

        try:
            with open(gradle_props, 'r', encoding='utf-8') as f:
                content = f.read()

            mc_version_match = re.search(r'minecraft_version\s*=\s*["\']?([0-9.]+)["\']?', content)
            if not mc_version_match:
                mc_version_match = re.search(r'mc_version\s*=\s*["\']?([0-9.]+)["\']?', content)

            if mc_version_match:
                found_version = mc_version_match.group(1)

                if self.config.strict_version:
                    if found_version != self.config.mc_version:
                        return False, f"minecraft_version is {found_version}, expected {self.config.mc_version} (strict mode)"
                    return True, f"\u2705 Exact version match: {found_version}"
                else:
                    if is_version_compatible(found_version, self.config.mc_version,
                                             strict=self.config.strict_version):
                        if found_version == self.config.mc_version:
                            return True, f"\u2705 Exact version match: {found_version}"
                        else:
                            return True, f"\u26a0\ufe0f  Close version match: {found_version} (target: {self.config.mc_version})"
                    else:
                        return False, f"minecraft_version is {found_version}, incompatible with {self.config.mc_version}"

            if not skip_loader_validation:
                loader_profile = get_profile(self.config.loader)
                if loader_profile.get("require_loader_version_in_gradle"):
                    found_loader_ver = False
                    for pattern in loader_profile["gradle_version_patterns"]:
                        if re.search(pattern, content):
                            found_loader_ver = True
                            break
                    if not found_loader_ver:
                        display = loader_profile["display_name"]
                        return False, f"{display} version not found in gradle.properties"

            return True, "gradle.properties validation passed"

        except Exception as e:
            return False, f"Error reading gradle.properties: {e}"

    def validate_build_gradle(self, repo_path: Path) -> Tuple[bool, str]:
        """Validate build.gradle for version compatibility."""
        build_gradle = repo_path / "build.gradle"
        build_gradle_kts = repo_path / "build.gradle.kts"

        gradle_file = build_gradle if build_gradle.exists() else build_gradle_kts if build_gradle_kts.exists() else None

        if not gradle_file:
            return True, "No build.gradle found (not critical)"

        try:
            with open(gradle_file, 'r', encoding='utf-8') as f:
                content = f.read()

            if self.config.mc_version in content:
                return True, "build.gradle mentions target version"

            return True, "build.gradle checked (version in properties)"

        except Exception as e:
            return False, f"Error reading build.gradle: {e}"
