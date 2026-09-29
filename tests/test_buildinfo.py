"""Tests for build-target extraction (modkeel/buildinfo.py) and judge_build_info.

Fixtures are trimmed from real branches that the old gradle.properties-only validator
rejected or accepted wrongly (2026-09 re-measurement against Modrinth).
"""

from unittest.mock import MagicMock

import pytest

from modkeel.buildinfo import (
    extract_build_info,
    find_mc_versions,
    loaders_from_tree,
    resolve_placeholders,
    select_build_files,
)
from modkeel.models import BranchCandidate, ModCompilerConfig
from modkeel.validation import NON_CODE_BRANCH, BranchValidator, is_bounded_range


def make_validator(mc="1.21.10", loader="neoforge"):
    config = ModCompilerConfig(mc_version=mc, loader=loader, loader_version="0")
    return BranchValidator(MagicMock(), config)


def judge(paths, files, mc="1.21.10", loader="neoforge", name="dev"):
    branch = BranchCandidate(name, "", "")
    ok = make_validator(mc, loader).judge_build_info(
        branch, extract_build_info(paths, files), loader)
    return ok, branch


class TestFindMcVersions:
    @pytest.mark.parametrize("content", [
        "minecraft_version=1.21.10",
        "mc_version = 1.21.10",
        "minecraftVersion=1.21.10",
        "mcVersion=1.21.10",
        'val MINECRAFT_VERSION: String = "1.21.10"',
        'val MINECRAFT_VERSION by extra { "1.21.10" }',
        '"minecraft_version" to "1.21.10"',
    ])
    def test_declaration_forms(self, content):
        assert find_mc_versions(content) == {"1.21.10"}

    @pytest.mark.parametrize("content", [
        "parchmentMinecraftVersion=1.21.10",
        "minecraftVersionRange=[1.21.10, 1.21.11)",
        "minecraftVersionRangeStart=1.21.10",
    ])
    def test_ignores_non_target_keys(self, content):
        assert find_mc_versions(content) == set()

    def test_year_based_versions(self):
        assert find_mc_versions("mcVersion=26.1") == {"26.1"}


class TestTreeLayout:
    def test_multiloader_subprojects(self):
        paths = ["NeoForge/build.gradle.kts", "Fabric/src/main/resources/fabric.mod.json",
                 "NeoForge/src/main/resources/META-INF/neoforge.mods.toml"]
        assert loaders_from_tree(paths) == {"neoforge", "fabric"}

    def test_stonecutter_dirs(self):
        paths = ["versions/26.1-fabric/gradle.properties",
                 "versions/26.1-neoforge/gradle.properties"]
        assert loaders_from_tree(paths) == {"fabric", "neoforge"}

    def test_test_resources_ignored(self):
        assert loaders_from_tree(["src/testmod/resources/fabric.mod.json"]) == set()

    def test_select_prefers_buildsrc_config(self):
        paths = ["gradle.properties", "buildSrc/src/main/kotlin/multiloader-base.gradle.kts",
                 "buildSrc/src/main/kotlin/BuildConfig.kt", "common/src/Foo.java",
                 "deep/nested/sub/gradle.properties"]
        selected = select_build_files(paths)
        assert "gradle.properties" in selected
        assert "common/src/Foo.java" not in selected
        assert "deep/nested/sub/gradle.properties" not in selected
        assert selected.index("buildSrc/src/main/kotlin/BuildConfig.kt") < \
            selected.index("buildSrc/src/main/kotlin/multiloader-base.gradle.kts")


class TestPlaceholders:
    def test_resolved(self):
        props = {"minecraft_maven_version_range": "[1.21.1,1.21.2)"}
        assert resolve_placeholders("${minecraft_maven_version_range}", props) == \
            "[1.21.1,1.21.2)"

    def test_unresolved_is_none(self):
        assert resolve_placeholders("[${minecraft_version}]", {}) is None


class TestBoundedRange:
    @pytest.mark.parametrize("r,bounded", [
        ("[1.21.3,)", False),
        ("[1.21.10, 1.21.11)", True),
        ("[1.21.10]", True),
        (">=1.21.2", False),
        (">=1.21.1 <1.21.2", True),
        ("1.21.4", True),
        ("~1.21.4", True),
    ])
    def test_bounded(self, r, bounded):
        assert is_bounded_range(r) is bounded


class TestNonCodeBranch:
    @pytest.mark.parametrize("name", ["l10n_crowdin_translations", "l10n_main",
                                      "dependabot/gradle/foo", "gh-pages", "vitepress-docs"])
    def test_rejected(self, name):
        assert NON_CODE_BRANCH.search(name)

    @pytest.mark.parametrize("name", ["1.21.10", "mc1.21.1/dev", "main", "port/26.1"])
    def test_kept(self, name):
        assert not NON_CODE_BRANCH.search(name)


class TestJudgeRealLayouts:
    def test_jei_camelcase(self):
        paths = ["gradle.properties", "NeoForge/build.gradle.kts", "Fabric/build.gradle.kts",
                 "NeoForge/src/main/resources/META-INF/neoforge.mods.toml"]
        files = {"gradle.properties": "minecraftVersion=1.21.10\n"
                                      "minecraftVersionRange=[1.21.10, 1.21.11)\n"
                                      "neoforgeVersion=21.10.64\n"}
        ok, b = judge(paths, files, name="1.21.10")
        assert ok and b.minecraft_version == "1.21.10" and b.loader == "neoforge"

    def test_sodium_buildsrc(self):
        paths = ["buildSrc/src/main/kotlin/BuildConfig.kt", "neoforge/build.gradle.kts",
                 "fabric/build.gradle.kts"]
        files = {"buildSrc/src/main/kotlin/BuildConfig.kt":
                 'val MINECRAFT_VERSION: String = "1.21.10"\n'
                 'val NEOFORGE_VERSION: String = "21.10.38-beta"\n'}
        ok, b = judge(paths, files, name="1.21.9/stable")
        assert ok and b.minecraft_version == "1.21.10"

    def test_iris_stale_open_range_does_not_accept_other_family(self):
        paths = ["build.gradle.kts", "neoforge/src/main/resources/META-INF/neoforge.mods.toml"]
        files = {
            "build.gradle.kts": 'val MINECRAFT_VERSION by extra { "26.2" }',
            "neoforge/src/main/resources/META-INF/neoforge.mods.toml":
                '[[dependencies.iris]]\nmodId = "minecraft"\nversionRange = "[1.21.3,)"\n',
        }
        ok, b = judge(paths, files, name="26.2")
        assert not ok and "mismatch" in b.validation_error

    def test_flywheel_placeholder_range_excludes_target(self):
        paths = ["gradle.properties", "neoforge/src/main/resources/META-INF/neoforge.mods.toml"]
        files = {
            "gradle.properties": "minecraft_version=1.21.1\n"
                                 "minecraft_maven_version_range=[1.21.1,1.21.2)\n"
                                 "neoforge_version=21.1.66\n",
            "neoforge/src/main/resources/META-INF/neoforge.mods.toml":
                '[[dependencies.flywheel]]\nmodId = "minecraft"\n'
                'versionRange = "${minecraft_maven_version_range}"\n',
        }
        ok, b = judge(paths, files, name="1.21.1/dev")
        assert not ok and "declared range" in b.validation_error

    def test_stonecutter_multi_target(self):
        paths = ["stonecutter.gradle.kts", "versions/1.21.10-neoforge/gradle.properties",
                 "versions/26.1-neoforge/gradle.properties"]
        files = {"versions/1.21.10-neoforge/gradle.properties": "mcVersion=1.21.10\n",
                 "versions/26.1-neoforge/gradle.properties": "mcVersion=26.1\n"}
        ok, b = judge(paths, files)
        assert ok and b.minecraft_version == "1.21.10"

    def test_fabric_only_rejected_for_neoforge(self):
        paths = ["gradle.properties", "src/main/resources/fabric.mod.json"]
        files = {"gradle.properties": "minecraft_version=1.21.10\nloader_version=0.17\n",
                 "src/main/resources/fabric.mod.json": '{"depends": {"minecraft": "1.21.10"}}'}
        ok, b = judge(paths, files, name="1.21.10/dev")
        assert not ok and b.validation_error.startswith("Fabric-only")

    def test_undetermined_but_branch_name_exact(self):
        paths = ["NeoForge/build.gradle"]
        ok, b = judge(paths, {}, name="1.21.10")
        assert ok and b.validation_method == "branch_name"

    def test_undetermined_rejected(self):
        ok, b = judge(["NeoForge/build.gradle"], {}, name="main")
        assert not ok and b.validation_error.startswith("Undetermined")


class TestModrinthSourceMatch:
    def test_filters_other_repos_and_tags_match(self, monkeypatch):
        from modkeel.modrinth import ModrinthClient

        projects = [
            {"id": "a", "source_url": "https://github.com/someone/iris-flw-compat"},
            {"id": "b", "source_url": "https://github.com/Engine-Room/Flywheel"},
            {"id": "c", "source_url": None},
        ]
        resp = MagicMock(status_code=200)
        resp.json.return_value = projects
        monkeypatch.setattr("modkeel.modrinth.requests.get", lambda *a, **k: resp)

        client = ModrinthClient(ModCompilerConfig("1.21.10", "neoforge", "0"))
        hits = [{"project_id": "a"}, {"project_id": "b"}, {"project_id": "c"}]
        kept = client._filter_hits_by_source(hits, "engine-room/Flywheel", {})

        assert [h["project_id"] for h in kept] == ["b", "c"]
        assert kept[0]["_source_match"] and "_source_match" not in kept[1]


class TestRangeOnlyAndPairs:
    TOML = "neoforge/src/main/resources/META-INF/neoforge.mods.toml"

    def _toml(self, r):
        return f'[[dependencies.x]]\nmodId = "minecraft"\nversionRange = "{r}"\n'

    def test_open_range_alone_is_undetermined(self):
        ok, b = judge([self.TOML], {self.TOML: self._toml("[1.21.3,)")}, name="craftmine")
        assert not ok and b.validation_error.startswith("Undetermined")

    def test_bounded_range_alone_accepts(self):
        ok, b = judge([self.TOML], {self.TOML: self._toml("[1.21.10,1.21.11)")})
        assert ok and b.validation_method == "metadata_range"

    def test_fabric_or_list_any_covers(self):
        path = "src/main/resources/fabric.mod.json"
        files = {path: '{"depends": {"minecraft": ["1.21.9", "1.21.10"]}}'}
        ok, _ = judge([path], files, loader="fabric")
        assert ok

    def test_stonecutter_pair_missing_loader(self):
        paths = ["versions/1.21.10-fabric/gradle.properties",
                 "versions/26.1-neoforge/gradle.properties"]
        ok, b = judge(paths, {})
        assert not ok and "only for fabric" in b.validation_error


class TestSecondRoundLayouts:
    """Gaps found by jev-lab exp16 on the 40 most-downloaded GitHub-hosted mods."""

    @pytest.mark.parametrize("content,expected", [
        ("neoMcVersion=1.21", {"1.21"}),
        ('val MINECRAFT_COMPILE_VERSION by extra { "26.2" }', {"26.2"}),
    ])
    def test_prefixed_keys(self, content, expected):
        assert find_mc_versions(content) == expected

    def test_catalog_prefer(self):
        info = extract_build_info(["gradle/libs.versions.toml"], {"gradle/libs.versions.toml":
                                  '[versions]\nminecraft = { strictly = "[26.1.2,)", prefer = "26.1.2" }\n'})
        assert info.mc_versions == {"26.1.2"}

    def test_loader_from_plugin_id(self):
        paths = ["build.gradle", "gradle.properties"]
        files = {"build.gradle": "plugins { id 'net.neoforged.gradle.userdev' version '7.1.38' }",
                 "gradle.properties": "minecraft_version=26.2\n"}
        ok, b = judge(paths, files, mc="26.2", name="26.2-neoforge")
        assert ok and b.loader == "neoforge"


def test_fabric_tilde_prerelease_suffix():
    from modkeel.version import is_version_in_fabric_range
    assert is_version_in_fabric_range("26.2", "~26.2-")
    assert not is_version_in_fabric_range("26.3", "~26.2-")
