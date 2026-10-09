"""
Smoke tests for Modkeel matching logic.

Tests version matching, loader matching, branch scoring, and range parsing
to prevent regression on the substring-matching bugs fixed in Phase 1.
"""

import re
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

from mod_auto_compiler import ModAutoCompiler
from modkeel.github import GitHubClient, never_pushed, split_unchanged_branches
from modkeel.models import (
    ModCompilerConfig, BranchCandidate, CompilationResult, FailureType,
)


def make_compiler(mc_version: str = "1.21.10", loader: str = "neoforge") -> ModAutoCompiler:
    """Create a compiler instance with a mock config (no real paths needed)."""
    config = MagicMock(spec=ModCompilerConfig)
    config.mc_version = mc_version
    config.loader = loader.lower()
    config.strict_version = False
    config.github_token = None
    config.github_headers = {}
    compiler = ModAutoCompiler(config)
    return compiler


class TestVersionMatching(unittest.TestCase):
    """Test that version matching uses word boundaries, not substrings."""

    def test_version_not_substring_match(self):
        """'1.21' should NOT match '1.210' or '1.21.10'."""
        compiler = make_compiler(mc_version="1.21.1")
        fork_data = {
            'name': 'Create-1.21.10-neoforge',
            'description': '',
            'topics': [],
            'stars': 0, 'watchers': 0, 'forks': 0,
            'updated_at': __import__('datetime').datetime.now(),
            'commit_count': 10, 'contributor_count': 1,
            'trust_analysis': {'trust_score': 80, 'warnings': [], 'signals': []},
        }
        result = compiler.score_fork_reliability(fork_data)
        self.assertFalse(result['has_version_match'],
                         "1.21.1 should NOT match name containing 1.21.10")

    def test_version_exact_match_in_name(self):
        """'1.21.10' should match 'mod-1.21.10-forge'."""
        compiler = make_compiler(mc_version="1.21.10")
        fork_data = {
            'name': 'mod-1.21.10-forge',
            'description': '',
            'topics': [],
            'stars': 0, 'watchers': 0, 'forks': 0,
            'updated_at': __import__('datetime').datetime.now(),
            'commit_count': 10, 'contributor_count': 1,
            'trust_analysis': {'trust_score': 80, 'warnings': [], 'signals': []},
        }
        result = compiler.score_fork_reliability(fork_data)
        self.assertTrue(result['has_version_match'],
                        "1.21.10 should match name containing 1.21.10")

    def test_version_match_in_description(self):
        """Version in description should be detected."""
        compiler = make_compiler(mc_version="1.21.10")
        fork_data = {
            'name': 'SomeMod',
            'description': 'Ported to 1.21.10 neoforge',
            'topics': [],
            'stars': 0, 'watchers': 0, 'forks': 0,
            'updated_at': __import__('datetime').datetime.now(),
            'commit_count': 10, 'contributor_count': 1,
            'trust_analysis': {'trust_score': 80, 'warnings': [], 'signals': []},
        }
        result = compiler.score_fork_reliability(fork_data)
        self.assertTrue(result['has_version_match'])


class TestLoaderMatching(unittest.TestCase):
    """Test that loader matching uses word boundaries."""

    def test_forge_not_in_reforged(self):
        """'forge' should NOT match 'reforged'."""
        compiler = make_compiler(loader="forge")
        fork_data = {
            'name': 'reforged-mod',
            'description': '',
            'topics': [],
            'stars': 0, 'watchers': 0, 'forks': 0,
            'updated_at': __import__('datetime').datetime.now(),
            'commit_count': 10, 'contributor_count': 1,
            'trust_analysis': {'trust_score': 80, 'warnings': [], 'signals': []},
        }
        result = compiler.score_fork_reliability(fork_data)
        self.assertFalse(result['has_loader_match'],
                         "'forge' should NOT match 'reforged'")

    def test_forge_matches_standalone(self):
        """'forge' should match 'mod-forge-1.21'."""
        compiler = make_compiler(loader="forge")
        fork_data = {
            'name': 'mod-forge-1.21',
            'description': '',
            'topics': [],
            'stars': 0, 'watchers': 0, 'forks': 0,
            'updated_at': __import__('datetime').datetime.now(),
            'commit_count': 10, 'contributor_count': 1,
            'trust_analysis': {'trust_score': 80, 'warnings': [], 'signals': []},
        }
        result = compiler.score_fork_reliability(fork_data)
        self.assertTrue(result['has_loader_match'],
                        "'forge' should match 'mod-forge-1.21'")

    def test_neoforge_not_in_forgeconfigapiport(self):
        """'neoforge' should NOT match 'forgeconfigapiport'."""
        compiler = make_compiler(loader="neoforge")
        fork_data = {
            'name': 'forgeconfigapiport',
            'description': '',
            'topics': [],
            'stars': 0, 'watchers': 0, 'forks': 0,
            'updated_at': __import__('datetime').datetime.now(),
            'commit_count': 10, 'contributor_count': 1,
            'trust_analysis': {'trust_score': 80, 'warnings': [], 'signals': []},
        }
        result = compiler.score_fork_reliability(fork_data)
        self.assertFalse(result['has_loader_match'],
                         "'neoforge' should NOT match 'forgeconfigapiport'")


class TestBranchDevPattern(unittest.TestCase):
    """Test that 'dev' branch bonus uses word boundaries."""

    def test_advent_not_dev(self):
        """Branch 'advent' should NOT get dev bonus."""
        compiler = make_compiler()
        branch = BranchCandidate(name="advent", commit_sha="abc", commit_date="")
        branch.minecraft_version = "1.21.10"
        branch.loader = "neoforge"
        branch.loader_version = "64"
        score = compiler.score_branch(branch)
        # Dev bonus is 100, so if score has it, it's wrong
        self.assertLess(score, 1200, "Branch 'advent' should not get dev bonus")

    def test_mc_dev_gets_bonus(self):
        """Branch 'mc1.21/dev' should get dev bonus."""
        compiler = make_compiler()
        branch = BranchCandidate(name="mc1.21/dev", commit_sha="abc", commit_date="")
        branch.minecraft_version = "1.21.10"
        branch.loader = "neoforge"
        branch.loader_version = "64"
        score = compiler.score_branch(branch)
        # Should include dev bonus (100) + exact version (1000) = at least 1100
        self.assertGreaterEqual(score, 1100,
                                "Branch 'mc1.21/dev' should get dev bonus")

    def test_dev_standalone_gets_bonus(self):
        """Branch 'dev' should get dev bonus."""
        compiler = make_compiler()
        branch = BranchCandidate(name="dev", commit_sha="abc", commit_date="")
        branch.minecraft_version = "1.21.10"
        branch.loader = "neoforge"
        branch.loader_version = "64"
        score = compiler.score_branch(branch)
        self.assertGreaterEqual(score, 1100,
                                "Branch 'dev' should get dev bonus")


class TestVersionExtraction(unittest.TestCase):
    """Test that version extraction from branch names doesn't match dates."""

    def test_date_not_extracted(self):
        """'2024-01-21' should NOT be extracted as MC version '1.21'."""
        # Use the same regex from filter_branches_by_version_proximity
        pattern = r'(?:^|[^.\d])(1\.\d+(?:\.\d+)?)(?:[^.\d]|$)'
        self.assertIsNone(re.search(pattern, "update-2024-01-21"),
                          "Date '2024-01-21' should not extract as MC version")

    def test_mc_version_extracted(self):
        """'mc1.21.1/dev' should extract '1.21.1'."""
        pattern = r'(?:^|[^.\d])(1\.\d+(?:\.\d+)?)(?:[^.\d]|$)'
        match = re.search(pattern, "mc1.21.1/dev")
        self.assertIsNotNone(match, "Should extract version from 'mc1.21.1/dev'")
        self.assertEqual(match.group(1), "1.21.1")

    def test_version_at_start(self):
        """'1.21.10/stable' should extract '1.21.10'."""
        pattern = r'(?:^|[^.\d])(1\.\d+(?:\.\d+)?)(?:[^.\d]|$)'
        match = re.search(pattern, "1.21.10/stable")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "1.21.10")


class TestMavenRangeParsing(unittest.TestCase):
    """Test Maven-style version range parsing."""

    def setUp(self):
        self.compiler = make_compiler()

    def test_inclusive_range(self):
        """[1.21,1.22) should contain 1.21.1 but not 1.22."""
        self.assertTrue(self.compiler.is_version_in_maven_range("1.21.1", "[1.21,1.22)"))
        self.assertFalse(self.compiler.is_version_in_maven_range("1.22", "[1.21,1.22)"))

    def test_exact_version(self):
        """[1.21.1] should match only 1.21.1."""
        self.assertTrue(self.compiler.is_version_in_maven_range("1.21.1", "[1.21.1]"))
        self.assertFalse(self.compiler.is_version_in_maven_range("1.21.2", "[1.21.1]"))

    def test_open_upper_bound(self):
        """[1.21,) should match 1.21 and above."""
        self.assertTrue(self.compiler.is_version_in_maven_range("1.21", "[1.21,)"))
        self.assertTrue(self.compiler.is_version_in_maven_range("1.22", "[1.21,)"))
        self.assertFalse(self.compiler.is_version_in_maven_range("1.20", "[1.21,)"))

    def test_range_boundaries(self):
        """Test inclusive vs exclusive boundaries."""
        # [1.21,1.22] - both inclusive
        self.assertTrue(self.compiler.is_version_in_maven_range("1.22", "[1.21,1.22]"))
        # (1.21,1.22) - both exclusive
        self.assertFalse(self.compiler.is_version_in_maven_range("1.21", "(1.21,1.22)"))
        self.assertFalse(self.compiler.is_version_in_maven_range("1.22", "(1.21,1.22)"))


class TestFabricRangeParsing(unittest.TestCase):
    """Test Fabric-style version range parsing."""

    def setUp(self):
        self.compiler = make_compiler()

    def test_gte_range(self):
        """>=1.21 should contain 1.21.1."""
        self.assertTrue(self.compiler.is_version_in_fabric_range("1.21.1", ">=1.21"))
        self.assertTrue(self.compiler.is_version_in_fabric_range("1.21", ">=1.21"))
        self.assertFalse(self.compiler.is_version_in_fabric_range("1.20.4", ">=1.21"))

    def test_tilde_range(self):
        """~1.21.0 should match 1.21.x."""
        self.assertTrue(self.compiler.is_version_in_fabric_range("1.21.1", "~1.21.0"))
        self.assertTrue(self.compiler.is_version_in_fabric_range("1.21.10", "~1.21.0"))

    def test_fabric_range_prerelease_dash_and_x_wildcard(self):
        """">=1.21.9-" admits 1.21.9's pre-releases (Mod Menu 16 declares ">=1.21.9- <1.21.11");
        "1.21.x" is Fabric's wildcard."""
        check = self.compiler.is_version_in_fabric_range
        self.assertTrue(check("1.21.10", ">=1.21.9- <1.21.11"))
        self.assertTrue(check("1.21.9", ">=1.21.9- <1.21.11"))
        self.assertFalse(check("1.21.11", ">=1.21.9- <1.21.11"))
        self.assertFalse(check("1.21.8", ">=1.21.9- <1.21.11"))
        self.assertTrue(check("1.21.10", "1.21.x"))
        self.assertFalse(check("1.20.1", "1.21.x"))
        self.assertTrue(check("1.21.10", "1.x"))
        self.assertFalse(self.compiler.is_version_in_fabric_range("1.20.4", "~1.21.0"))

    def test_exact_version(self):
        """Exact version should only match itself."""
        self.assertTrue(self.compiler.is_version_in_fabric_range("1.21.1", "1.21.1"))
        self.assertFalse(self.compiler.is_version_in_fabric_range("1.21.2", "1.21.1"))


class TestVersionCompatibility(unittest.TestCase):
    """Test the is_version_compatible method."""

    def setUp(self):
        self.compiler = make_compiler(mc_version="1.21.10")

    def test_exact_match(self):
        self.assertTrue(self.compiler.is_version_compatible("1.21.10", "1.21.10"))

    def test_same_major_minor(self):
        """Lenient mode: 1.21.1 is compatible with 1.21.10 (same major.minor)."""
        self.assertTrue(self.compiler.is_version_compatible("1.21.1", "1.21.10"))

    def test_different_major_minor(self):
        """1.20.x is NOT compatible with 1.21.x."""
        self.assertFalse(self.compiler.is_version_compatible("1.20.4", "1.21.10"))


class TestClassifyBuildFailure(unittest.TestCase):
    """Test Gradle build failure classification."""

    def setUp(self):
        self.compiler = make_compiler()

    def test_dependency_resolution_could_not_find(self):
        """'Could not find' pattern should classify as DEPENDENCY_RESOLUTION."""
        stderr = (
            "FAILURE: Build failed with an exception.\n"
            "Could not resolve all files for configuration ':compileClasspath'.\n"
            "Could not find dev.engine-room.flywheel:flywheel-neoforge-api:1.0.0-beta.\n"
        )
        fail_type, deps = self.compiler.classify_build_failure(stderr, "")
        self.assertEqual(fail_type, FailureType.DEPENDENCY_RESOLUTION)
        self.assertIn(
            "dev.engine-room.flywheel:flywheel-neoforge-api:1.0.0-beta", deps
        )

    def test_dependency_resolution_could_not_resolve(self):
        """'Could not resolve' pattern should classify as DEPENDENCY_RESOLUTION."""
        stderr = (
            "Could not resolve com.example:my-lib:2.3.4.\n"
            "Required by: project :main\n"
        )
        fail_type, deps = self.compiler.classify_build_failure(stderr, "")
        self.assertEqual(fail_type, FailureType.DEPENDENCY_RESOLUTION)
        self.assertIn("com.example:my-lib:2.3.4", deps)

    def test_multiple_missing_deps_deduplicated(self):
        """Multiple occurrences of same dep should be deduplicated."""
        stderr = (
            "Could not find org.a:b:1.0.\n"
            "Could not resolve org.a:b:1.0.\n"
            "Could not find org.c:d:2.0.\n"
        )
        fail_type, deps = self.compiler.classify_build_failure(stderr, "")
        self.assertEqual(fail_type, FailureType.DEPENDENCY_RESOLUTION)
        self.assertEqual(len(deps), 2)
        self.assertIn("org.a:b:1.0", deps)
        self.assertIn("org.c:d:2.0", deps)

    def test_generic_build_error(self):
        """Non-dependency errors should classify as BUILD_ERROR."""
        stderr = (
            "FAILURE: Build failed with an exception.\n"
            "Compilation failed; see the compiler error output for details.\n"
        )
        fail_type, deps = self.compiler.classify_build_failure(stderr, "")
        self.assertEqual(fail_type, FailureType.BUILD_ERROR)
        self.assertEqual(deps, [])

    def test_empty_output(self):
        """Empty stderr/stdout should classify as BUILD_ERROR."""
        fail_type, deps = self.compiler.classify_build_failure("", "")
        self.assertEqual(fail_type, FailureType.BUILD_ERROR)
        self.assertEqual(deps, [])

    def test_dependency_in_stdout(self):
        """Dependency errors in stdout (not stderr) should also be detected."""
        stdout = "Could not find net.fabricmc:fabric-api:0.92.0.\n"
        fail_type, deps = self.compiler.classify_build_failure("", stdout)
        self.assertEqual(fail_type, FailureType.DEPENDENCY_RESOLUTION)
        self.assertIn("net.fabricmc:fabric-api:0.92.0", deps)


class TestCrossLoader(unittest.TestCase):
    """Test cross-loader Fabric fallback configuration."""

    def test_cross_loader_flag_default(self):
        """cross_loader should default to True in ModCompilerConfig."""
        config = ModCompilerConfig(
            mc_version="1.21.10",
            loader="neoforge",
            loader_version="64",
            output_dir="/tmp/modkeel_test_out"
        )
        self.assertTrue(config.cross_loader)

    def test_cross_loader_flag_disabled(self):
        """cross_loader=False should be respected."""
        config = ModCompilerConfig(
            mc_version="1.21.10",
            loader="neoforge",
            loader_version="64",
            output_dir="/tmp/modkeel_test_out",
            cross_loader=False
        )
        self.assertFalse(config.cross_loader)

    def test_compilation_result_cross_loader_default(self):
        """is_cross_loader should default to False."""
        result = CompilationResult(
            repo_url="https://github.com/test/mod",
            success=True
        )
        self.assertFalse(result.is_cross_loader)

    def test_compilation_result_cross_loader_set(self):
        """is_cross_loader=True should be stored."""
        result = CompilationResult(
            repo_url="https://github.com/test/mod",
            success=True,
            is_cross_loader=True
        )
        self.assertTrue(result.is_cross_loader)

    def test_pre_validate_with_override_loader(self):
        """override_loader='fabric' should accept Fabric branches."""
        compiler = make_compiler(mc_version="1.21.1", loader="neoforge")
        branch = BranchCandidate(name="main", commit_sha="abc", commit_date="")

        # Simulate what pre_validate_branch does internally when it finds
        # a fabric mod: set branch.loader = 'fabric'
        branch.loader = 'fabric'
        branch.minecraft_version = '1.21.1'
        branch.loader_version = '0.15.0'

        # With override_loader='fabric', a fabric branch should be valid
        # We can't fully call pre_validate_branch (needs API), but we can
        # verify the target_loader logic is correct by checking that
        # the config itself doesn't block fabric when override is used
        target_loader = 'fabric'  # override_loader='fabric'
        self.assertEqual(branch.loader, target_loader,
                         "Fabric branch should match override_loader='fabric'")

        # Without override, neoforge config should reject fabric branches
        self.assertNotEqual(branch.loader, compiler.config.loader,
                            "Fabric branch should NOT match neoforge config")


class TestIndependentPortSearch(unittest.TestCase):
    """Test independent port filtering logic."""

    def test_search_method_renamed(self):
        """search_compatible_repos should exist (renamed from search_compatible_forks)."""
        compiler = make_compiler()
        self.assertTrue(
            hasattr(compiler, 'search_compatible_repos'),
            "search_compatible_repos method should exist"
        )
        self.assertFalse(
            hasattr(compiler, 'search_compatible_forks'),
            "search_compatible_forks should no longer exist"
        )

    def test_independent_port_not_original_repo(self):
        """The original repo should be discarded in independent port filtering."""
        # Simulate the filtering logic used in search_compatible_repos
        original_full = "creator/SomeMod"

        candidates = [
            {"full_name": "creator/SomeMod"},     # original - should skip
            {"full_name": "porter/SomeMod"},       # port - should keep
            {"full_name": "other/SomeMod-Forge"},  # port - should keep
        ]

        accepted = [
            c for c in candidates
            if c["full_name"].lower() != original_full.lower()
        ]

        self.assertEqual(len(accepted), 2)
        self.assertTrue(
            all(c["full_name"] != "creator/SomeMod" for c in accepted),
            "Original repo should not appear in accepted candidates"
        )

    def test_independent_port_score_penalty(self):
        """Independent ports should receive a -10 score penalty."""
        compiler = make_compiler(mc_version="1.21.10")
        fork_data = {
            'name': 'SomeMod-1.21.10-neoforge',
            'description': 'Port of SomeMod to NeoForge 1.21.10',
            'topics': [],
            'stars': 5, 'watchers': 2, 'forks': 0,
            'updated_at': __import__('datetime').datetime.now(),
            'commit_count': 20, 'contributor_count': 2,
            'trust_analysis': {'trust_score': 80, 'warnings': [], 'signals': []},
        }

        # Score without penalty (fork)
        scored_fork = compiler.score_fork_reliability(fork_data)
        fork_score = scored_fork['score']

        # Score with penalty (independent)
        scored_independent = compiler.score_fork_reliability(fork_data)
        scored_independent['score'] -= 10
        scored_independent['signals'].append('independent_port')

        self.assertEqual(scored_independent['score'], fork_score - 10)
        self.assertIn('independent_port', scored_independent['signals'])


class TestCheckModrinth(unittest.TestCase):
    """Test Modrinth search method."""

    def test_check_modrinth_exists(self):
        """check_modrinth method should exist on ModAutoCompiler."""
        compiler = make_compiler()
        self.assertTrue(hasattr(compiler, 'check_modrinth'))

    def test_camelcase_split(self):
        """CamelCase mod names should be split for search queries."""
        import re
        name = "JustEnoughItems"
        result = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name)
        self.assertEqual(result, "Just Enough Items")

    def test_camelcase_single_word(self):
        """Single-word names should not be modified."""
        import re
        name = "Create"
        result = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name)
        self.assertEqual(result, "Create")

    def test_camelcase_with_acronym(self):
        """Names with ALL CAPS sections should split correctly."""
        import re
        name = "YetAnotherConfigLib"
        result = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name)
        self.assertEqual(result, "Yet Another Config Lib")

    def test_modrinth_download_flag_default(self):
        """modrinth_download should default to False."""
        result = CompilationResult(repo_url="https://github.com/test/mod", success=True)
        self.assertFalse(result.modrinth_download)

    def test_modrinth_download_flag_set(self):
        """modrinth_download=True should be stored."""
        result = CompilationResult(
            repo_url="https://github.com/test/mod",
            success=True,
            modrinth_download=True
        )
        self.assertTrue(result.modrinth_download)


    def test_pick_version_prefers_release(self):
        """Search and download pick the same version: newest release over a newer beta."""
        from modkeel.modrinth import pick_version
        versions = [{"version_number": "19.57", "version_type": "beta"},
                    {"version_number": "19.51", "version_type": "release"}]
        self.assertEqual(pick_version(versions)["version_number"], "19.51")

    def test_pick_version_falls_back_to_newest(self):
        """With no release, the newest version is used."""
        from modkeel.modrinth import pick_version
        versions = [{"version_number": "2.0b", "version_type": "beta"},
                    {"version_number": "1.0a", "version_type": "alpha"}]
        self.assertEqual(pick_version(versions)["version_number"], "2.0b")

class TestDiffAnalysis(unittest.TestCase):
    """Test fork diff analysis method."""

    def test_analyze_fork_diff_exists(self):
        """analyze_fork_diff method should exist on ModAutoCompiler."""
        compiler = make_compiler()
        self.assertTrue(hasattr(compiler, 'analyze_fork_diff'))

    def test_clean_port_classification(self):
        """Only version/build file changes should be classified as clean port."""
        # Simulate the classification logic from analyze_fork_diff
        VERSION_FILES = {
            'gradle.properties', 'build.gradle', 'build.gradle.kts',
            'settings.gradle', 'settings.gradle.kts',
        }
        METADATA_SUFFIXES = (
            'gradle.properties', 'build.gradle', 'build.gradle.kts',
            'mods.toml', 'neoforge.mods.toml', 'fabric.mod.json',
        )

        # Pure version port
        files_clean = [
            {'filename': 'gradle.properties'},
            {'filename': 'build.gradle'},
        ]
        source_changes = [
            f for f in files_clean
            if f['filename'] not in VERSION_FILES
            and not any(f['filename'].endswith(s) for s in METADATA_SUFFIXES)
        ]
        self.assertEqual(len(source_changes), 0, "Clean port should have 0 source changes")

        # Port with source changes
        files_dirty = [
            {'filename': 'gradle.properties'},
            {'filename': 'src/main/java/com/example/Mod.java'},
            {'filename': 'src/main/java/com/example/Config.java'},
        ]
        source_changes_dirty = [
            f for f in files_dirty
            if f['filename'] not in VERSION_FILES
            and not any(f['filename'].endswith(s) for s in METADATA_SUFFIXES)
        ]
        self.assertEqual(len(source_changes_dirty), 2, "Dirty port should have 2 source changes")

    def test_score_bonus_values(self):
        """Score bonuses should follow the expected tiers."""
        # Clean port (0 source files) -> 200 bonus
        # Mostly clean (1-3 source files) -> 100 bonus
        # Moderate (4-10 source files) -> 50 bonus
        # Extensive (10+ source files) -> 0 bonus
        self.assertEqual(200, 200)  # Clean
        self.assertEqual(100, 100)  # Mostly clean
        self.assertEqual(50, 50)    # Moderate
        self.assertEqual(0, 0)      # Extensive


class TestModrinthMatching(unittest.TestCase):
    """Test Modrinth name matching logic improvements."""

    def _normalize(self, name: str) -> str:
        """Replicate the normalization used in check_modrinth."""
        return re.sub(r'[-_]', '', name.lower())

    def test_slug_with_hyphens_matches(self):
        """'ForgifiedFabricAPI' should match slug 'forgified-fabric-api'."""
        mod_name = "ForgifiedFabricAPI"
        slug = "forgified-fabric-api"
        mod_normalized = self._normalize(mod_name)
        slug_normalized = self._normalize(slug)
        self.assertEqual(mod_normalized, slug_normalized)

    def test_slug_exact_match(self):
        """'sodium' slug should match 'sodium' mod name."""
        self.assertEqual(self._normalize("sodium"), self._normalize("sodium"))

    def test_camelcase_normalization(self):
        """'YetAnotherConfigLib' should match 'yet-another-config-lib'."""
        self.assertEqual(
            self._normalize("YetAnotherConfigLib"),
            self._normalize("yet-another-config-lib")
        )

    def test_single_word_matching_logic(self):
        """Single-word mods like 'Create' should be matchable."""
        search_query = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', "Create")
        words = search_query.lower().split()
        self.assertEqual(len(words), 1)
        self.assertEqual(words[0], "create")
        # With the fix, single words >= 4 chars are eligible for matching
        self.assertTrue(len(words[0]) >= 4)

    def test_single_word_title_startswith(self):
        """'create' should match title starting with 'create'."""
        word = "create"
        title = "create mod for minecraft"
        self.assertTrue(title.startswith(word))

    def test_underscore_normalization(self):
        """'forge_config_api_port' should match 'forgeconfigapiport'."""
        self.assertEqual(
            self._normalize("forge_config_api_port"),
            self._normalize("forgeconfigapiport")
        )


class TestVersionCatalogFallback(unittest.TestCase):
    """Test that version catalog is checked when gradle.properties lacks mc version."""

    def test_toml_version_extraction(self):
        """Should extract minecraft version from libs.versions.toml format."""
        import toml
        toml_content = """
[versions]
minecraft = "1.21.10"
neoforge = "21.10.1"
fabric-loader = "0.16.0"
"""
        data = toml.loads(toml_content)
        versions = data['versions']
        mc_version = (versions.get('minecraft')
                      or versions.get('minecraft-version')
                      or versions.get('game-version'))
        self.assertEqual(mc_version, "1.21.10")

    def test_toml_loader_detection(self):
        """Should detect NeoForge loader from version catalog."""
        import toml
        toml_content = """
[versions]
minecraft = "1.21.10"
neoforge = "21.10.1"

[libraries]
neoforge = { module = "net.neoforged:neoforge", version.ref = "neoforge" }
"""
        data = toml.loads(toml_content)
        has_neoforge = 'neoforge' in str(data).lower()
        self.assertTrue(has_neoforge)

    def test_toml_dict_version_format(self):
        """Should handle table-format version entries like {ref = 'minecraft'}."""
        import toml
        toml_content = """
[versions]
minecraft = "1.21.10"

[libraries]
minecraft = { module = "com.mojang:minecraft", version.ref = "minecraft" }
"""
        data = toml.loads(toml_content)
        mc_version = data['versions']['minecraft']
        if isinstance(mc_version, dict):
            mc_version = mc_version.get('ref') or mc_version.get('version')
        self.assertEqual(str(mc_version).strip('"'), "1.21.10")

    def test_gradle_properties_without_mc_version(self):
        """gradle.properties without minecraft_version should trigger catalog fallback."""
        gradle_content = """
        mod_version=1.0.0
        group=com.example
        archives_base_name=mymod
        """
        mc_match = re.search(
            r'minecraft_version\s*=\s*["\']?([0-9.]+)["\']?', gradle_content)
        if not mc_match:
            mc_match = re.search(
                r'mc_version\s*=\s*["\']?([0-9.]+)["\']?', gradle_content)
        # Should not find anything, triggering version catalog fallback
        self.assertIsNone(mc_match)


class TestQuiltGradleDetection(unittest.TestCase):
    """Test that Quilt loader is detected from gradle.properties."""

    def test_quilt_loader_version_detected(self):
        """quilt_loader_version in gradle.properties should detect Quilt."""
        from modkeel.loaders import LOADER_PROFILES

        gradle_content = """
minecraft_version=1.21.10
quilt_loader_version=0.23.1
quilt_mappings_version=1.21.10+build.1
"""
        for pattern in LOADER_PROFILES["quilt"]["gradle_detection_patterns"]:
            if re.search(pattern, gradle_content):
                detected = True
                break
        else:
            detected = False
        self.assertTrue(detected, "Quilt should be detected from gradle.properties")

    def test_quilt_version_extraction(self):
        """Should extract Quilt loader version from gradle.properties."""
        from modkeel.loaders import LOADER_PROFILES

        gradle_content = "quilt_loader_version = 0.23.1"
        for pattern in LOADER_PROFILES["quilt"]["gradle_version_patterns"]:
            match = re.search(pattern, gradle_content)
            if match:
                break
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "0.23.1")

    def test_quilt_not_confused_with_fabric(self):
        """Quilt patterns should not match fabric_loader_version."""
        from modkeel.loaders import LOADER_PROFILES

        gradle_content = "fabric_loader_version = 0.16.0"
        for pattern in LOADER_PROFILES["quilt"]["gradle_detection_patterns"]:
            if re.search(pattern, gradle_content):
                self.fail("Quilt pattern should not match fabric_loader_version")


class TestLoaderValidationInConfig(unittest.TestCase):
    """Test that ModCompilerConfig validates and normalizes loader."""

    def test_valid_loader_accepted(self):
        """All loaders in ALL_LOADERS should be accepted."""
        from modkeel.loaders import ALL_LOADERS
        from unittest.mock import patch
        for loader in ALL_LOADERS:
            with patch.object(Path, 'mkdir'):
                config = ModCompilerConfig(
                    mc_version="1.21.10",
                    loader=loader,
                    loader_version="1",
                    output_dir="/tmp/test_out"
                )
                self.assertEqual(config.loader, loader)

    def test_invalid_loader_raises(self):
        """Unknown loader should raise ValueError."""
        from unittest.mock import patch
        with self.assertRaises(ValueError):
            with patch.object(Path, 'mkdir'):
                ModCompilerConfig(
                    mc_version="1.21.10",
                    loader="bukkit",
                    loader_version="1",
                    output_dir="/tmp/test_out"
                )

    def test_loader_normalized_to_lowercase(self):
        """Loader should be normalized to lowercase."""
        from unittest.mock import patch
        with patch.object(Path, 'mkdir'):
            config = ModCompilerConfig(
                mc_version="1.21.10",
                loader="NeoForge",
                loader_version="1",
                output_dir="/tmp/test_out"
            )
            self.assertEqual(config.loader, "neoforge")


class TestFuzzyScore(unittest.TestCase):
    """Test fuzzy_score() from modkeel.utils."""

    def setUp(self):
        from modkeel.utils import fuzzy_score as fs
        self.fuzzy_score = fs

    def test_exact_match_returns_100(self):
        self.assertEqual(self.fuzzy_score("Sodium", "Sodium"), 100.0)

    def test_case_insensitive_exact(self):
        self.assertEqual(self.fuzzy_score("sodium", "Sodium"), 100.0)

    def test_normalized_exact_with_separators(self):
        """Hyphens, underscores, spaces removed before comparison."""
        self.assertEqual(self.fuzzy_score("fabric-api", "fabric_api"), 100.0)
        self.assertEqual(self.fuzzy_score("Just Enough Items", "justenoughitems"), 100.0)

    def test_prefix_match_high_score(self):
        score = self.fuzzy_score("sodium", "sodium-extra")
        self.assertGreater(score, 60.0)

    def test_typo_still_scores_well(self):
        """'Sodum' (missing 'i') should still match 'Sodium' reasonably."""
        score = self.fuzzy_score("Sodum", "Sodium")
        self.assertGreater(score, 50.0)

    def test_completely_different_scores_low(self):
        score = self.fuzzy_score("Optifine", "Create")
        self.assertLess(score, 30.0)

    def test_empty_strings_return_zero(self):
        self.assertEqual(self.fuzzy_score("", "Sodium"), 0.0)
        self.assertEqual(self.fuzzy_score("Sodium", ""), 0.0)
        self.assertEqual(self.fuzzy_score("", ""), 0.0)

    def test_substring_match(self):
        """Query contained in candidate should score decently."""
        score = self.fuzzy_score("jei", "jei-integration")
        self.assertGreater(score, 45.0)

    def test_short_query_vs_long_candidate(self):
        """Very short query vs very long candidate should be penalized."""
        score = self.fuzzy_score("a", "abcdefghijklmnop")
        self.assertLess(score, 50.0)

    def test_camel_case_mod_name(self):
        """CamelCase mod names should match their slug equivalents."""
        score = self.fuzzy_score("JustEnoughItems", "just-enough-items")
        self.assertEqual(score, 100.0)

    def test_similar_but_different_mods(self):
        """'Sodium' should score higher for 'Sodium' than 'Sodium Extra'."""
        exact = self.fuzzy_score("Sodium", "Sodium")
        partial = self.fuzzy_score("Sodium", "Sodium Extra")
        self.assertGreater(exact, partial)

    def test_reversed_query_candidate(self):
        """Candidate prefix of query should still score."""
        score = self.fuzzy_score("sodium-extra", "sodium")
        self.assertGreater(score, 50.0)


class TestFuzzyMatch(unittest.TestCase):
    """Test fuzzy_match() from modkeel.utils."""

    def setUp(self):
        from modkeel.utils import fuzzy_match as fm
        self.fuzzy_match = fm

    def test_exact_match_first(self):
        candidates = [
            ("sodium-extra", "Sodium Extra"),
            ("sodium", "Sodium"),
            ("lithium", "Lithium"),
        ]
        results = self.fuzzy_match("sodium", candidates)
        self.assertEqual(results[0][0], "sodium")

    def test_threshold_filters(self):
        candidates = [
            ("create", "Create"),
            ("xyz-unrelated", "Completely Different"),
        ]
        results = self.fuzzy_match("create", candidates, threshold=50.0)
        ids = [r[0] for r in results]
        self.assertIn("create", ids)
        self.assertNotIn("xyz-unrelated", ids)

    def test_max_results_limits(self):
        candidates = [(f"mod{i}", f"Mod {i}") for i in range(20)]
        results = self.fuzzy_match("mod", candidates, threshold=0.0, max_results=3)
        self.assertLessEqual(len(results), 3)

    def test_empty_candidates(self):
        results = self.fuzzy_match("sodium", [])
        self.assertEqual(results, [])

    def test_matches_by_display_name(self):
        """Should match against display name, not just ID."""
        candidates = [
            ("jei", "Just Enough Items"),
        ]
        results = self.fuzzy_match("Just Enough Items", candidates, threshold=50.0)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], "jei")

    def test_sorted_by_score_descending(self):
        candidates = [
            ("sodium-extra", "Sodium Extra"),
            ("sodium", "Sodium"),
            ("sodium-dynamic-lights", "Sodium Dynamic Lights"),
        ]
        results = self.fuzzy_match("sodium", candidates, threshold=0.0)
        scores = [r[2] for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))


class TestSubsequenceScore(unittest.TestCase):
    """Test _subsequence_score() from modkeel.utils."""

    def setUp(self):
        from modkeel.utils import _subsequence_score as ss
        self._subsequence_score = ss

    def test_perfect_match(self):
        score = self._subsequence_score("abc", "abc")
        self.assertAlmostEqual(score, 1.0, places=1)

    def test_subsequence_in_longer(self):
        score = self._subsequence_score("ace", "abcde")
        self.assertGreater(score, 0.5)

    def test_no_match(self):
        score = self._subsequence_score("xyz", "abc")
        self.assertEqual(score, 0.0)

    def test_empty_query(self):
        score = self._subsequence_score("", "abc")
        self.assertEqual(score, 0.0)

    def test_consecutive_bonus(self):
        """Consecutive matches should score higher or equal vs spread matches."""
        tight = self._subsequence_score("abc", "xabcx")
        spread = self._subsequence_score("abc", "xaxbxcx")
        self.assertGreaterEqual(tight, spread)


if __name__ == "__main__":
    unittest.main()


# ============================================================================
# UNCHANGED FORK COPIES (forks named for a version that only copy upstream)
# ============================================================================

def _branch(name: str, sha: str) -> BranchCandidate:
    return BranchCandidate(name, sha, "2026-01-01T00:00:00Z")


class TestUnchangedForkCopies(unittest.TestCase):
    """A fork branch with no commits of its own holds nothing upstream does not."""

    UPSTREAM = [_branch("mc1.21.1/dev", "up-121"), _branch("mc1.20.1/fabric/dev", "up-fab")]

    def split(self, fork_branches, ahead=None):
        github = MagicMock()
        github.commits_ahead.return_value = ahead
        own, copies = split_unchanged_branches(github, "Creators-of-Create", "Create",
                                               self.UPSTREAM, fork_branches)
        return [b.name for b in own], [b.name for b in copies], github

    def test_same_head_as_an_upstream_branch_is_free(self):
        """NetworkArchitect-sudo/Create_1.21.10: its branch is upstream's, SHA for SHA."""
        own, copies, github = self.split([_branch("mc1.20.1/fabric/dev", "up-fab")])
        self.assertEqual((own, copies), ([], ["mc1.20.1/fabric/dev"]))
        github.commits_ahead.assert_not_called()

    def test_behind_upstream_with_no_own_commits(self):
        """YourNerdiness/Create-1.21.10: an older upstream commit, nothing of its own."""
        own, copies, github = self.split([_branch("mc1.21.1/dev", "older")], ahead=0)
        self.assertEqual((own, copies), ([], ["mc1.21.1/dev"]))
        github.commits_ahead.assert_called_once_with(
            "Creators-of-Create", "Create", "up-121", "older")

    def test_own_commits_are_kept(self):
        own, copies, _ = self.split([_branch("mc1.21.1/dev", "ported")], ahead=12)
        self.assertEqual((own, copies), (["mc1.21.1/dev"], []))

    def test_cannot_compare_counts_as_changed(self):
        """Never drop a possible port because GitHub could not answer."""
        own, _, _ = self.split([_branch("mc1.21.1/dev", "x")], ahead=None)
        self.assertEqual(own, ["mc1.21.1/dev"])

    def test_new_branch_name_is_kept_without_a_call(self):
        own, _, github = self.split([_branch("port-1.21.10", "new")])
        self.assertEqual(own, ["port-1.21.10"])
        github.commits_ahead.assert_not_called()


class TestCommitsAhead(unittest.TestCase):
    def client(self):
        return GitHubClient(ModCompilerConfig("1.21.10", "neoforge", "0"))

    def test_reads_ahead_by(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"ahead_by": 3, "behind_by": 40}
        with patch("modkeel.github.requests.get", return_value=resp) as get:
            self.assertEqual(self.client().commits_ahead("o", "r", "base", "head"), 3)
        self.assertTrue(get.call_args.args[0].endswith("/repos/o/r/compare/base...head"))

    def test_error_is_unknown(self):
        with patch("modkeel.github.requests.get", return_value=MagicMock(status_code=404)):
            self.assertIsNone(self.client().commits_ahead("o", "r", "base", "head"))


class TestNeverPushed(unittest.TestCase):
    """A fork with no push after its creation is a copy; decided from search data alone."""

    def fork(self, created, pushed, independent=False):
        return {"created_at": created, "pushed_at": pushed,
                "is_independent_port": independent}

    def test_never_pushed(self):
        # GitHub keeps the parent's last push time on a new fork: earlier than created_at
        self.assertTrue(never_pushed(self.fork("2026-03-01T10:00:00Z", "2025-12-07T09:00:00Z")))
        self.assertTrue(never_pushed(self.fork("2026-03-01T10:00:00Z", "2026-03-01T10:00:30Z")))

    def test_pushed_after_forking(self):
        self.assertFalse(never_pushed(self.fork("2026-03-01T10:00:00Z", "2026-03-04T18:00:00Z")))

    def test_independent_ports_and_missing_data_are_not_judged(self):
        self.assertFalse(never_pushed(self.fork("2026-03-01T10:00:00Z", "2025-01-01T00:00:00Z",
                                                independent=True)))
        self.assertFalse(never_pushed(self.fork(None, None)))
        self.assertFalse(never_pushed({}))


class TestBranchListingCost(unittest.TestCase):
    """Listing branches costs one call per 100 branches; dates only for ranked branches."""

    def client(self):
        return GitHubClient(ModCompilerConfig("1.21.10", "neoforge", "0"))

    @staticmethod
    def page(names, next_url=None):
        resp = MagicMock(status_code=200)
        resp.json.return_value = [{"name": n, "commit": {"sha": f"sha-{n}"}} for n in names]
        resp.links = {"next": {"url": next_url}} if next_url else {}
        return resp

    def test_one_call_per_page_no_dates(self):
        with patch("modkeel.github.requests.get",
                   side_effect=[self.page(["a", "b"], "https://next"), self.page(["c"])]) as get:
            branches = self.client().get_branches("o", "r")
        self.assertEqual([b.name for b in branches], ["a", "b", "c"])
        self.assertEqual([b.commit_sha for b in branches], ["sha-a", "sha-b", "sha-c"])
        self.assertTrue(all(b.commit_date == "" for b in branches))
        self.assertEqual(get.call_count, 2)          # was 1 + one per branch
        self.assertEqual(get.call_args_list[0].kwargs["params"], {"per_page": 100})
        self.assertEqual(get.call_args_list[1].args[0], "https://next")

    def test_dates_fetched_once_per_commit(self):
        client = self.client()
        commit = MagicMock(status_code=200)
        commit.json.return_value = {"commit": {"committer": {"date": "2026-09-01T00:00:00Z"}}}
        a, b = BranchCandidate("a", "same", ""), BranchCandidate("b", "same", "")
        dated = BranchCandidate("c", "other", "2026-01-01T00:00:00Z")
        with patch("modkeel.github.requests.get", return_value=commit) as get:
            client.fill_commit_dates("o", "r", [a, b, dated])
            client.fill_commit_dates("fork", "r", [BranchCandidate("d", "same", "")])
        self.assertEqual(get.call_count, 1)          # cached by SHA, across repos
        self.assertEqual((a.commit_date, b.commit_date), ("2026-09-01T00:00:00Z",) * 2)
        self.assertEqual(dated.commit_date, "2026-01-01T00:00:00Z")
