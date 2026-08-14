"""Tests for the Level 0.5 bytecode linkage check."""

import struct
import zipfile
from pathlib import Path

import pytest

from modforge.linkage import (
    SCHEME_INTERMEDIARY,
    SCHEME_MOJANG,
    SCHEME_SRG,
    ClassFileError,
    ClassRefs,
    LinkageReport,
    check_jar,
    check_refs,
    detect_naming_scheme,
    extract_refs,
    internal_to_fqcn,
    parse_constant_pool,
    read_jar_refs,
)
from modforge.symbols import parse_proguard_mappings

SAMPLE_MAPPINGS = """\
net.minecraft.world.level.Level -> dcw:
    int MAX_LEVEL_SIZE -> k
    171:171:boolean isClientSide() -> x_
net.minecraft.core.BlockPos -> ji:
    net.minecraft.core.BlockPos ZERO -> g
com.mojang.blaze3d.vertex.PoseStack -> gce:
    5:5:void pushPose() -> a
"""


@pytest.fixture
def index():
    return parse_proguard_mappings(SAMPLE_MAPPINGS, "1.21.1")


# ── Class file synthesis ─────────────────────────────────────────────────────


def build_class_file(entries):
    """Assemble a minimal class file carrying the given constant pool entries.

    ``entries`` are (tag, payload_bytes) pairs; index 0 is the reserved slot.
    """
    body = b""
    slots = 1  # index 0 is reserved
    for tag, payload in entries:
        body += bytes([tag]) + payload
        # Longs and doubles consume two constant pool slots, so the declared count is
        # not simply the number of entries.
        slots += 2 if tag in (5, 6) else 1
    return (
        b"\xca\xfe\xba\xbe"
        + struct.pack(">HH", 0, 65)
        + struct.pack(">H", slots)
        + body
    )


def utf8_entry(text: str):
    encoded = text.encode("utf-8")
    return (1, struct.pack(">H", len(encoded)) + encoded)


def simple_class_ref(internal_name: str):
    """A class file whose pool holds one Utf8 plus a CONSTANT_Class pointing at it."""
    return build_class_file([utf8_entry(internal_name), (7, struct.pack(">H", 1))])


# ── Constant pool parsing ────────────────────────────────────────────────────


class TestParseConstantPool:
    def test_rejects_non_class_file(self):
        with pytest.raises(ClassFileError):
            parse_constant_pool(b"PK\x03\x04 definitely a zip")

    def test_rejects_truncated_file(self):
        with pytest.raises(ClassFileError):
            parse_constant_pool(b"\xca\xfe\xba\xbe\x00")

    def test_reads_utf8_entry(self):
        pool = parse_constant_pool(build_class_file([utf8_entry("hello")]))
        assert pool[1] == (1, "hello")

    def test_rejects_unknown_tag(self):
        with pytest.raises(ClassFileError):
            parse_constant_pool(build_class_file([(99, b"\x00\x00")]))

    def test_rejects_truncated_utf8_payload(self):
        broken = (
            b"\xca\xfe\xba\xbe"
            + struct.pack(">HH", 0, 65)
            + struct.pack(">H", 2)
            + bytes([1])
            + struct.pack(">H", 50)
            + b"short"
        )
        with pytest.raises(ClassFileError):
            parse_constant_pool(broken)

    def test_long_occupies_two_slots(self):
        # A Long followed by a Utf8: the Utf8 must land at index 3, not 2.
        data = build_class_file([(5, b"\x00" * 8), utf8_entry("after")])
        pool = parse_constant_pool(data)
        assert pool[2] is None
        assert pool[3] == (1, "after")

    def test_double_occupies_two_slots(self):
        pool = parse_constant_pool(
            build_class_file([(6, b"\x00" * 8), utf8_entry("after")])
        )
        assert pool[3] == (1, "after")


# ── Name conversion ──────────────────────────────────────────────────────────


class TestInternalToFqcn:
    def test_plain_class(self):
        assert internal_to_fqcn("net/minecraft/core/BlockPos") == (
            "net.minecraft.core.BlockPos"
        )

    def test_object_array(self):
        assert internal_to_fqcn("[Lnet/minecraft/core/BlockPos;") == (
            "net.minecraft.core.BlockPos"
        )

    def test_nested_array(self):
        assert internal_to_fqcn("[[Ljava/lang/String;") == "java.lang.String"

    def test_primitive_array_returns_none(self):
        assert internal_to_fqcn("[I") is None

    def test_inner_class_keeps_dollar(self):
        assert internal_to_fqcn("net/minecraft/Foo$Bar") == "net.minecraft.Foo$Bar"


# ── Reference extraction ─────────────────────────────────────────────────────


class TestExtractRefs:
    def test_extracts_class_reference(self):
        refs = extract_refs(simple_class_ref("net/minecraft/world/level/Level"))
        assert "net.minecraft.world.level.Level" in refs.classes
        assert refs.classes_parsed == 1

    def test_extracts_methodref_with_descriptor(self):
        data = build_class_file(
            [
                utf8_entry("net/minecraft/world/level/Level"),  # 1
                (7, struct.pack(">H", 1)),  # 2 Class
                utf8_entry("isClientSide"),  # 3
                utf8_entry("()Z"),  # 4
                (12, struct.pack(">HH", 3, 4)),  # 5 NameAndType
                (10, struct.pack(">HH", 2, 5)),  # 6 Methodref
            ]
        )
        refs = extract_refs(data)
        assert (
            "net.minecraft.world.level.Level",
            "isClientSide",
            "()Z",
        ) in refs.methods

    def test_extracts_fieldref(self):
        data = build_class_file(
            [
                utf8_entry("net/minecraft/core/BlockPos"),
                (7, struct.pack(">H", 1)),
                utf8_entry("ZERO"),
                utf8_entry("Lnet/minecraft/core/BlockPos;"),
                (12, struct.pack(">HH", 3, 4)),
                (9, struct.pack(">HH", 2, 5)),
            ]
        )
        refs = extract_refs(data)
        assert (
            "net.minecraft.core.BlockPos",
            "ZERO",
            "Lnet/minecraft/core/BlockPos;",
        ) in (refs.fields)

    def test_merge_accumulates(self):
        a = ClassRefs(classes={"a"}, classes_parsed=1)
        b = ClassRefs(classes={"b"}, classes_parsed=1)
        a.merge(b)
        assert a.classes == {"a", "b"}
        assert a.classes_parsed == 2


# ── Naming scheme detection ──────────────────────────────────────────────────


class TestDetectNamingScheme:
    def test_mojang_names(self):
        refs = ClassRefs(classes={"net.minecraft.world.level.Level"})
        assert detect_naming_scheme(refs) == SCHEME_MOJANG

    def test_intermediary_names(self):
        refs = ClassRefs(classes={"net.minecraft.class_1937"})
        assert detect_naming_scheme(refs) == SCHEME_INTERMEDIARY

    def test_srg_member_names(self):
        refs = ClassRefs(
            classes={"net.minecraft.world.level.Level"},
            methods={("net.minecraft.world.level.Level", "m_46805_", "()Z")},
        )
        assert detect_naming_scheme(refs) == SCHEME_SRG

    def test_srg_field_names(self):
        refs = ClassRefs(
            classes={"net.minecraft.world.level.Level"},
            fields={("net.minecraft.world.level.Level", "f_46443_", "I")},
        )
        assert detect_naming_scheme(refs) == SCHEME_SRG

    def test_intermediary_wins_over_srg(self):
        refs = ClassRefs(
            classes={"net.minecraft.class_1937"},
            methods={("net.minecraft.class_1937", "m_1_", "()V")},
        )
        assert detect_naming_scheme(refs) == SCHEME_INTERMEDIARY


# ── check_refs ───────────────────────────────────────────────────────────────


class TestCheckRefs:
    def test_all_classes_resolve(self, index):
        refs = ClassRefs(
            classes={"net.minecraft.world.level.Level", "net.minecraft.core.BlockPos"},
            classes_parsed=2,
        )
        report = check_refs(refs, index)
        assert report.is_clean
        assert report.refs_checked == 2

    def test_missing_class_is_reported(self, index):
        refs = ClassRefs(
            classes={
                "net.minecraft.world.level.Level",
                "net.minecraft.core.BlockPos",
                "net.minecraft.core.Gone",
            },
            classes_parsed=3,
        )
        report = check_refs(refs, index)
        assert report.missing_classes == ["net.minecraft.core.Gone"]
        assert not report.is_clean

    def test_intermediary_jar_is_skipped(self, index):
        refs = ClassRefs(classes={"net.minecraft.class_1937"}, classes_parsed=1)
        report = check_refs(refs, index)
        assert not report.checked
        assert report.scheme == SCHEME_INTERMEDIARY
        assert "intermediary" in report.skip_reason

    def test_srg_jar_is_skipped(self, index):
        refs = ClassRefs(
            classes={"net.minecraft.world.level.Level"},
            methods={("net.minecraft.world.level.Level", "m_46805_", "()Z")},
            classes_parsed=1,
        )
        assert not check_refs(refs, index).checked

    def test_uncovered_packages_are_ignored(self, index):
        # Brigadier ships unobfuscated and is absent from mappings entirely.
        refs = ClassRefs(
            classes={
                "net.minecraft.core.BlockPos",
                "com.mojang.brigadier.CommandDispatcher",
            },
            classes_parsed=2,
        )
        report = check_refs(refs, index)
        assert report.is_clean
        assert report.refs_checked == 1

    def test_third_party_classes_are_ignored(self, index):
        refs = ClassRefs(
            classes={"net.minecraft.core.BlockPos", "org.apache.commons.Thing"},
            classes_parsed=2,
        )
        assert check_refs(refs, index).is_clean

    def test_no_minecraft_refs_is_skipped(self, index):
        refs = ClassRefs(classes={"java.util.List"}, classes_parsed=1)
        report = check_refs(refs, index)
        assert not report.checked
        assert "no checkable" in report.skip_reason

    def test_wholesale_absence_assumes_misnaming(self, index):
        refs = ClassRefs(
            classes={f"net.minecraft.core.Missing{i}" for i in range(10)}
            | {"net.minecraft.core.BlockPos"},
            classes_parsed=11,
        )
        report = check_refs(refs, index)
        assert not report.checked
        assert "unreadable naming scheme" in report.skip_reason

    def test_members_are_never_checked(self, index):
        # Inherited members cannot be resolved without a class hierarchy, which Mojang
        # mappings do not provide, so member references must not produce findings.
        refs = ClassRefs(
            classes={"net.minecraft.world.level.Level"},
            methods={
                ("net.minecraft.world.level.Level", "inheritedFromSupertype", "()V")
            },
            fields={("net.minecraft.world.level.Level", "INHERITED_CONSTANT", "I")},
            classes_parsed=1,
        )
        assert check_refs(refs, index).is_clean


# ── JAR-level ────────────────────────────────────────────────────────────────


class TestCheckJar:
    def _make_jar(self, path: Path, entries: dict):
        with zipfile.ZipFile(path, "w") as archive:
            for name, payload in entries.items():
                archive.writestr(name, payload)
        return path

    def test_reads_classes_from_jar(self, tmp_path):
        jar = self._make_jar(
            tmp_path / "m.jar",
            {"pkg/A.class": simple_class_ref("net/minecraft/core/BlockPos")},
        )
        refs = read_jar_refs(jar)
        assert refs.classes_parsed == 1
        assert "net.minecraft.core.BlockPos" in refs.classes

    def test_skips_multi_release_entries(self, tmp_path):
        jar = self._make_jar(
            tmp_path / "m.jar",
            {
                "pkg/A.class": simple_class_ref("net/minecraft/core/BlockPos"),
                "META-INF/versions/17/pkg/A.class": simple_class_ref(
                    "net/minecraft/core/Gone"
                ),
            },
        )
        refs = read_jar_refs(jar)
        assert refs.classes_parsed == 1
        assert "net.minecraft.core.Gone" not in refs.classes

    def test_ignores_non_class_entries(self, tmp_path):
        jar = self._make_jar(
            tmp_path / "m.jar",
            {
                "META-INF/mods.toml": "modId='x'",
                "pkg/A.class": simple_class_ref("net/minecraft/core/BlockPos"),
            },
        )
        assert read_jar_refs(jar).classes_parsed == 1

    def test_corrupt_class_is_skipped_not_fatal(self, tmp_path):
        jar = self._make_jar(
            tmp_path / "m.jar",
            {
                "pkg/Bad.class": b"garbage",
                "pkg/Good.class": simple_class_ref("net/minecraft/core/BlockPos"),
            },
        )
        refs = read_jar_refs(jar)
        assert refs.classes_parsed == 1

    def test_respects_max_classes(self, tmp_path):
        entries = {
            f"pkg/A{i}.class": simple_class_ref("net/minecraft/core/BlockPos")
            for i in range(5)
        }
        jar = self._make_jar(tmp_path / "m.jar", entries)
        assert read_jar_refs(jar, max_classes=2).classes_parsed == 2

    def test_unreadable_jar_is_skipped(self, tmp_path, index):
        path = tmp_path / "not.jar"
        path.write_bytes(b"definitely not a zip")
        report = check_jar(path, index)
        assert not report.checked
        assert "unreadable" in report.skip_reason

    def test_classless_jar_is_skipped(self, tmp_path, index):
        jar = self._make_jar(tmp_path / "m.jar", {"README.txt": "hi"})
        report = check_jar(jar, index)
        assert not report.checked
        assert "no class files" in report.skip_reason

    def test_clean_jar_passes(self, tmp_path, index):
        jar = self._make_jar(
            tmp_path / "m.jar",
            {"pkg/A.class": simple_class_ref("net/minecraft/core/BlockPos")},
        )
        assert check_jar(jar, index).is_clean

    def test_jar_targeting_wrong_version_fails(self, tmp_path, index):
        entries = {
            "pkg/Ok.class": simple_class_ref("net/minecraft/core/BlockPos"),
            "pkg/A.class": simple_class_ref("net/minecraft/core/BrandNewThing"),
        }
        jar = self._make_jar(tmp_path / "m.jar", entries)
        report = check_jar(jar, index)
        assert report.checked
        assert "net.minecraft.core.BrandNewThing" in report.missing_classes


class TestLinkageReport:
    def test_skipped_is_not_clean(self):
        assert not LinkageReport.skipped("reason").is_clean

    def test_skipped_summary_mentions_reason(self):
        assert "reason" in LinkageReport.skipped("reason").summary

    def test_findings_are_human_readable(self):
        report = LinkageReport(mc_version="1.21.1", missing_classes=["net.minecraft.X"])
        assert report.findings == ["class not in 1.21.1: net.minecraft.X"]
