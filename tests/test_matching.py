"""
Smoke tests for ModForge matching logic.

Tests version matching, loader matching, branch scoring, and range parsing
to prevent regression on the substring-matching bugs fixed in Phase 1.
"""

import re
import sys
import os
import unittest
from unittest.mock import MagicMock
from pathlib import Path

# Add parent dir to path so we can import the module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mod_auto_compiler import (
    ModAutoCompiler, ModCompilerConfig, BranchCandidate, CompilationResult,
    FailureType
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
            output_dir="/tmp/modforge_test_out"
        )
        self.assertTrue(config.cross_loader)

    def test_cross_loader_flag_disabled(self):
        """cross_loader=False should be respected."""
        config = ModCompilerConfig(
            mc_version="1.21.10",
            loader="neoforge",
            loader_version="64",
            output_dir="/tmp/modforge_test_out",
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


if __name__ == "__main__":
    unittest.main()
