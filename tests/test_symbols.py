"""Tests for the Level 1.5 symbol check: mappings, symbol index, source scanning."""

import gzip
import json
from pathlib import Path

import pytest

from modforge.javascan import SourceRefs, scan_source, scan_tree
from modforge.mappings import (
    FLAVOR_MCP,
    FLAVOR_MOJMAP,
    FLAVOR_YARN,
    cache_path,
    detect_flavor,
)
from modforge.symbols import (
    BONUS_CLEAN,
    SymbolIndex,
    SymbolReport,
    check_references,
    is_candidate,
    method_descriptor,
    package_of,
    parse_proguard_mappings,
    split_params,
    to_descriptor,
)

# A miniature slice of real Mojang mappings, same shape as the published file.
SAMPLE_MAPPINGS = """\
# comment line
net.minecraft.world.level.Level -> dcw:
    int MAX_LEVEL_SIZE -> k
    171:171:boolean isClientSide() -> x_
    181:181:boolean isInWorldBounds(net.minecraft.core.BlockPos) -> k
    200:210:void setBlock(net.minecraft.core.BlockPos,int) -> m
    101:167:void <init>(net.minecraft.core.BlockPos) -> <init>
net.minecraft.core.BlockPos -> ji:
    net.minecraft.core.BlockPos ZERO -> g
    12:12:long asLong() -> t
com.mojang.blaze3d.vertex.PoseStack -> gce:
    5:5:void pushPose() -> a
org.somelibrary.Ignored -> zz:
    1:1:void thing() -> a
"""


@pytest.fixture
def index():
    return parse_proguard_mappings(SAMPLE_MAPPINGS, "1.21.1")


# ── Descriptor conversion ────────────────────────────────────────────────────


class TestDescriptors:
    @pytest.mark.parametrize(
        "java,expected",
        [
            ("int", "I"),
            ("void", "V"),
            ("boolean", "Z"),
            ("long", "J"),
            ("double", "D"),
            ("net.minecraft.core.BlockPos", "Lnet/minecraft/core/BlockPos;"),
            ("int[]", "[I"),
            ("java.lang.String[][]", "[[Ljava/lang/String;"),
        ],
    )
    def test_to_descriptor(self, java, expected):
        assert to_descriptor(java) == expected

    def test_method_descriptor_no_params(self):
        assert method_descriptor("boolean", []) == "()Z"

    def test_method_descriptor_with_params(self):
        assert (
            method_descriptor("void", ["net.minecraft.core.BlockPos", "int"])
            == "(Lnet/minecraft/core/BlockPos;I)V"
        )

    def test_split_params_empty(self):
        assert split_params("") == []
        assert split_params("   ") == []

    def test_split_params_multiple(self):
        assert split_params("int, boolean") == ["int", "boolean"]

    def test_package_of(self):
        assert package_of("net.minecraft.core.BlockPos") == "net.minecraft.core"
        assert package_of("NoPackage") == ""


# ── Mappings parsing ─────────────────────────────────────────────────────────


class TestParseMappings:
    def test_indexes_minecraft_classes(self, index):
        assert index.has_class("net.minecraft.world.level.Level")
        assert index.has_class("net.minecraft.core.BlockPos")

    def test_indexes_mojang_client_classes(self, index):
        assert index.has_class("com.mojang.blaze3d.vertex.PoseStack")

    def test_ignores_third_party_classes(self, index):
        assert not index.has_class("org.somelibrary.Ignored")

    def test_indexes_fields(self, index):
        assert index.has_field("net.minecraft.world.level.Level", "MAX_LEVEL_SIZE")
        assert not index.has_field("net.minecraft.world.level.Level", "NOPE")

    def test_indexes_methods_with_arity(self, index):
        lvl = "net.minecraft.world.level.Level"
        assert index.has_method(lvl, "isClientSide", 0)
        assert index.has_method(lvl, "isInWorldBounds", 1)
        assert index.has_method(lvl, "setBlock", 2)
        assert not index.has_method(lvl, "isInWorldBounds", 2)

    def test_method_lookup_without_arity(self, index):
        assert index.has_method("net.minecraft.world.level.Level", "setBlock")

    def test_skips_constructors(self, index):
        assert not index.has_method("net.minecraft.world.level.Level", "<init>")

    def test_builds_full_descriptors(self, index):
        lvl = "net.minecraft.world.level.Level"
        assert index.has_descriptor(lvl, "isClientSide", "()Z")
        assert index.has_descriptor(
            lvl, "isInWorldBounds", "(Lnet/minecraft/core/BlockPos;)Z"
        )
        assert not index.has_descriptor(lvl, "isInWorldBounds", "()Z")

    def test_has_member_spans_fields_and_methods(self, index):
        lvl = "net.minecraft.world.level.Level"
        assert index.has_member(lvl, "MAX_LEVEL_SIZE")
        assert index.has_member(lvl, "isClientSide")
        assert not index.has_member(lvl, "nonexistent")


class TestPackageCoverage:
    def test_covers_indexed_package(self, index):
        assert index.covers("net.minecraft.core.Anything")

    def test_does_not_cover_absent_package(self, index):
        # Brigadier and DataFixerUpper ship unobfuscated and are absent from mappings.
        assert not index.covers("com.mojang.brigadier.CommandDispatcher")
        assert not index.covers("com.mojang.datafixers.util.Either")

    def test_candidate_prefilter_is_coarser_than_coverage(self, index):
        fqcn = "com.mojang.brigadier.CommandDispatcher"
        assert is_candidate(fqcn)
        assert not index.covers(fqcn)


# ── Persistence ──────────────────────────────────────────────────────────────


class TestIndexPersistence:
    def test_roundtrip(self, index, tmp_path):
        path = tmp_path / "idx.json.gz"
        index.save(path)
        loaded = SymbolIndex.load(path)

        assert loaded is not None
        assert loaded.classes == index.classes
        assert loaded.methods == index.methods
        assert loaded.fields == index.fields
        assert loaded.descriptors == index.descriptors
        assert loaded.mc_version == "1.21.1"

    def test_missing_file_returns_none(self, tmp_path):
        assert SymbolIndex.load(tmp_path / "absent.json.gz") is None

    def test_corrupt_file_returns_none(self, tmp_path):
        path = tmp_path / "bad.json.gz"
        path.write_bytes(b"not gzip at all")
        assert SymbolIndex.load(path) is None

    def test_stale_format_returns_none(self, index, tmp_path):
        path = tmp_path / "old.json.gz"
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump({"format": 0, "flavor": "mojmap", "mc_version": "1.21.1"}, f)
        assert SymbolIndex.load(path) is None

    def test_cache_path_includes_flavor_and_version(self):
        assert cache_path("1.21.1", "mojmap").name == "mojmap-1.21.1.json.gz"


# ── Flavor detection ─────────────────────────────────────────────────────────


class TestDetectFlavor:
    def test_yarn_marker_wins(self):
        script = """
        dependencies { mappings "net.fabricmc:yarn:1.21.1+build.3:v2" }
        // also mentions neoforge for architectury
        """
        assert detect_flavor([script]) == FLAVOR_YARN

    def test_neoforge_implies_mojmap(self):
        # Modern NeoForge builds never write officialMojangMappings() explicitly.
        assert detect_flavor(["implementation 'net.neoforged:neoforge:21.1.0'"]) == (
            FLAVOR_MOJMAP
        )

    def test_moddevgradle_implies_mojmap(self):
        assert (
            detect_flavor(["id 'net.neoforged.moddev' version '2.0'"]) == FLAVOR_MOJMAP
        )

    def test_parchment_implies_mojmap(self):
        assert detect_flavor(["parchment = '2024.07.28'"]) == FLAVOR_MOJMAP

    def test_explicit_mojang_mappings(self):
        assert (
            detect_flavor(["mappings loom.officialMojangMappings()"]) == FLAVOR_MOJMAP
        )

    def test_mcp_channel(self):
        assert detect_flavor(["mappings channel: 'snapshot', version: '2021'"]) == (
            FLAVOR_MCP
        )

    def test_fabric_loader_defaults_to_yarn(self):
        assert detect_flavor([""], loader="fabric") == FLAVOR_YARN

    def test_unknown_defaults_to_mojmap(self):
        assert detect_flavor([""], loader="neoforge") == FLAVOR_MOJMAP

    def test_handles_none_entries(self):
        assert detect_flavor([None, "", None], loader="neoforge") == FLAVOR_MOJMAP


# ── Source scanning ──────────────────────────────────────────────────────────


class TestScanSource:
    def test_extracts_imports(self):
        refs = scan_source(
            "import net.minecraft.world.level.Level;\n"
            "import static net.minecraft.core.BlockPos.ZERO;\n"
        )
        assert "net.minecraft.world.level.Level" in refs.imports
        assert "net.minecraft.core.BlockPos.ZERO" in refs.imports

    def test_extracts_mixin_class_literal(self):
        refs = scan_source("@Mixin(Level.class)\npublic class LevelMixin {}")
        assert "Level" in refs.mixin_targets

    def test_extracts_multiple_mixin_targets(self):
        refs = scan_source("@Mixin({Level.class, BlockPos.class})")
        assert {"Level", "BlockPos"} <= refs.mixin_targets

    def test_extracts_mixin_string_targets(self):
        refs = scan_source('@Mixin(targets = "net.minecraft.world.level.Level")')
        assert "net.minecraft.world.level.Level" in refs.mixin_targets

    def test_extracts_at_method_target(self):
        refs = scan_source(
            '@At(value = "INVOKE", target = '
            '"Lnet/minecraft/world/level/Level;isInWorldBounds'
            '(Lnet/minecraft/core/BlockPos;)Z")'
        )
        assert (
            "net.minecraft.world.level.Level",
            "isInWorldBounds",
            "(Lnet/minecraft/core/BlockPos;)Z",
        ) in refs.at_targets

    def test_extracts_at_field_target(self):
        refs = scan_source(
            '@At(value = "FIELD", target = '
            '"Lnet/minecraft/core/BlockPos;ZERO:Lnet/minecraft/core/BlockPos;")'
        )
        owners = {owner for owner, _, _ in refs.at_targets}
        assert "net.minecraft.core.BlockPos" in owners

    def test_records_declared_classes(self):
        refs = scan_source(
            "package net.minecraft.core;\n"
            "public interface DuckHelper { }\n"
        )
        assert "net.minecraft.core.DuckHelper" in refs.declared_classes

    def test_records_multiple_declared_types(self):
        refs = scan_source(
            "package com.example;\npublic class A {}\nenum B {}\nrecord C() {}\n"
        )
        assert {"com.example.A", "com.example.B", "com.example.C"} <= refs.declared_classes

    def test_no_package_means_no_declared_classes(self):
        assert scan_source("public class Loose {}").declared_classes == set()

    def test_detects_preprocessor(self):
        refs = scan_source("//#if MC>=11904\nimport net.minecraft.X;\n//#endif")
        assert refs.uses_preprocessor

    def test_clean_source_has_no_preprocessor_flag(self):
        assert not scan_source("import net.minecraft.X;").uses_preprocessor


class TestSourceRefs:
    def test_resolve_simple_name_via_imports(self):
        refs = SourceRefs(imports={"net.minecraft.world.level.Level"})
        assert refs.resolve("Level") == "net.minecraft.world.level.Level"

    def test_resolve_unknown_name_returns_none(self):
        assert SourceRefs(imports=set()).resolve("Mystery") is None

    def test_resolve_passes_through_qualified_names(self):
        refs = SourceRefs()
        assert refs.resolve("net.minecraft.core.BlockPos") == (
            "net.minecraft.core.BlockPos"
        )

    def test_merge_accumulates(self):
        a = SourceRefs(imports={"a"}, files_scanned=1)
        b = SourceRefs(imports={"b"}, files_scanned=1, uses_preprocessor=True)
        a.merge(b)
        assert a.imports == {"a", "b"}
        assert a.files_scanned == 2
        assert a.uses_preprocessor


class TestScanTree:
    def test_detects_multi_version_source_sets(self, tmp_path):
        for variant in ("java", "java21"):
            d = tmp_path / "src" / "main" / variant / "pkg"
            d.mkdir(parents=True)
            (d / "A.java").write_text("import net.minecraft.world.level.Level;")
        refs = scan_tree(tmp_path)
        assert refs.multi_version_sources

    def test_single_source_set_is_not_flagged(self, tmp_path):
        d = tmp_path / "src" / "main" / "java" / "pkg"
        d.mkdir(parents=True)
        (d / "A.java").write_text("import net.minecraft.world.level.Level;")
        refs = scan_tree(tmp_path)
        assert not refs.multi_version_sources
        assert refs.files_scanned == 1

    def test_respects_max_files(self, tmp_path):
        d = tmp_path / "src" / "main" / "java"
        d.mkdir(parents=True)
        for i in range(5):
            (d / f"A{i}.java").write_text("import net.minecraft.core.BlockPos;")
        assert scan_tree(tmp_path, max_files=2).files_scanned == 2


# ── check_references ─────────────────────────────────────────────────────────


class TestCheckReferences:
    def test_clean_source_scores_positive(self, index):
        refs = SourceRefs(
            imports={"net.minecraft.world.level.Level", "net.minecraft.core.BlockPos"}
        )
        report = check_references(refs, index)
        assert report.is_clean
        assert report.score_delta == BONUS_CLEAN
        assert report.classes_checked == 2

    def test_missing_class_is_reported(self, index):
        refs = SourceRefs(
            imports={
                "net.minecraft.world.level.Level",
                "net.minecraft.core.BlockPos",
                "net.minecraft.core.Gone",
            }
        )
        report = check_references(refs, index)
        assert report.missing_classes == ["net.minecraft.core.Gone"]
        assert report.score_delta < 0

    def test_uncovered_package_is_not_reported_missing(self, index):
        refs = SourceRefs(
            imports={
                "net.minecraft.world.level.Level",
                "com.mojang.brigadier.CommandDispatcher",
                "com.mojang.datafixers.util.Either",
            }
        )
        report = check_references(refs, index)
        assert report.is_clean
        assert report.classes_checked == 1

    def test_third_party_imports_ignored(self, index):
        refs = SourceRefs(
            imports={"net.minecraft.core.BlockPos", "org.apache.commons.Whatever"}
        )
        assert check_references(refs, index).is_clean

    def test_wholesale_absence_assumes_wrong_flavor(self, index):
        refs = SourceRefs(
            imports={f"net.minecraft.core.Missing{i}" for i in range(10)}
            | {"net.minecraft.core.BlockPos"}
        )
        report = check_references(refs, index)
        assert not report.checked
        assert "wrong mapping flavor" in report.skip_reason
        assert report.score_delta == 0

    def test_preprocessor_skips_check(self, index):
        refs = SourceRefs(imports={"net.minecraft.core.Gone"}, uses_preprocessor=True)
        report = check_references(refs, index)
        assert not report.checked
        assert "preprocessor" in report.skip_reason

    def test_multi_version_sources_skips_check(self, index):
        refs = SourceRefs(
            imports={"net.minecraft.core.Gone"}, multi_version_sources=True
        )
        assert not check_references(refs, index).checked

    def test_no_minecraft_imports_skips_check(self, index):
        assert not check_references(
            SourceRefs(imports={"java.util.List"}), index
        ).checked

    def test_missing_mixin_target(self, index):
        refs = SourceRefs(
            imports={"net.minecraft.core.BlockPos", "net.minecraft.core.Ghost"},
            mixin_targets={"BlockPos"},
        )
        refs.imports.discard("net.minecraft.core.Ghost")
        refs.mixin_targets.add("net.minecraft.core.Ghost")
        report = check_references(refs, index)
        assert "net.minecraft.core.Ghost" in report.missing_mixin_targets

    def test_valid_at_descriptor_passes(self, index):
        refs = SourceRefs(
            imports={"net.minecraft.world.level.Level"},
            at_targets={
                (
                    "net.minecraft.world.level.Level",
                    "isInWorldBounds",
                    "(Lnet/minecraft/core/BlockPos;)Z",
                )
            },
        )
        assert check_references(refs, index).is_clean

    def test_at_descriptor_mismatch_is_not_reported(self, index):
        # Mixin resolves @At targets through the class hierarchy at runtime, so a target
        # naming a method declared on a supertype is legal. ProGuard mappings carry no
        # hierarchy, making member-level verdicts undecidable -- only the owner class is
        # checked. Measured on Create mc1.21.1/dev: descriptor checking produced 12
        # findings, every one inherited or NeoForge-patched.
        refs = SourceRefs(
            imports={"net.minecraft.world.level.Level"},
            at_targets={
                ("net.minecraft.world.level.Level", "inheritedFromEntity", "()V")
            },
        )
        assert check_references(refs, index).is_clean

    def test_at_target_on_missing_class_is_reported(self, index):
        refs = SourceRefs(
            imports={"net.minecraft.core.BlockPos"},
            at_targets={("net.minecraft.core.Vanished", "whatever", "()V")},
        )
        report = check_references(refs, index)
        assert report.missing_at_targets == ["net.minecraft.core.Vanished.whatever"]

    def test_self_declared_class_is_not_missing(self, index):
        # Duck interfaces and accessors are routinely declared inside net.minecraft.*
        # packages by the mod itself; they are the mod's own code, not absent Minecraft.
        refs = SourceRefs(
            imports={"net.minecraft.core.BlockPos", "net.minecraft.core.DuckHelper"},
            declared_classes={"net.minecraft.core.DuckHelper"},
        )
        assert check_references(refs, index).is_clean

    def test_self_declared_mixin_target_is_ignored(self, index):
        refs = SourceRefs(
            imports={"net.minecraft.core.BlockPos"},
            mixin_targets={"net.minecraft.core.OwnDuck"},
            declared_classes={"net.minecraft.core.OwnDuck"},
        )
        assert check_references(refs, index).is_clean

    def test_unresolvable_mixin_name_is_ignored(self, index):
        refs = SourceRefs(
            imports={"net.minecraft.core.BlockPos"}, mixin_targets={"SomeUnknownThing"}
        )
        assert check_references(refs, index).is_clean

    def test_missing_static_member(self, index):
        refs = SourceRefs(
            imports={"net.minecraft.core.BlockPos"},
            static_refs={("net.minecraft.core.BlockPos", "NOT_A_FIELD")},
        )
        report = check_references(refs, index)
        assert report.missing_members == ["net.minecraft.core.BlockPos.NOT_A_FIELD"]

    def test_existing_static_member_passes(self, index):
        refs = SourceRefs(
            imports={"net.minecraft.core.BlockPos"},
            static_refs={("net.minecraft.core.BlockPos", "ZERO")},
        )
        assert check_references(refs, index).is_clean


class TestSymbolReport:
    def test_skipped_contributes_no_score(self):
        assert SymbolReport.skipped("because").score_delta == 0

    def test_skipped_is_not_clean(self):
        assert not SymbolReport.skipped("because").is_clean

    def test_summary_mentions_skip_reason(self):
        assert "because" in SymbolReport.skipped("because").summary

    def test_findings_are_human_readable(self):
        report = SymbolReport(mc_version="1.21.1", missing_classes=["net.minecraft.X"])
        assert report.findings == ["class not in 1.21.1: net.minecraft.X"]
