"""Tests for mixin scanning, on class files assembled byte by byte."""

import io
import struct
import zipfile

from modkeel.mixinscan import jar_profile, parse_mixin, selector_name

from tests.test_memberlink import Pool

MIXIN = "Lorg/spongepowered/asm/mixin/Mixin;"
INJECT = "Lorg/spongepowered/asm/mixin/injection/Inject;"
REDIRECT = "Lorg/spongepowered/asm/mixin/injection/Redirect;"
OVERWRITE = "Lorg/spongepowered/asm/mixin/Overwrite;"


def annotations(p, anns):
    """RuntimeInvisibleAnnotations attribute: [(type, {key: [strings]} | {key: class})]."""
    body = struct.pack(">H", len(anns))
    for kind, values in anns:
        body += struct.pack(">HH", p.utf8(kind), len(values))
        for key, value in values.items():
            body += struct.pack(">H", p.utf8(key))
            if isinstance(value, int):  # int constant, e.g. require = 1
                body += b"I" + struct.pack(">H", p._add(("i", value),
                                                        b"\x03" + struct.pack(">i", value)))
            elif isinstance(value, tuple):  # class literals
                body += b"[" + struct.pack(">H", len(value))
                for c in value:
                    body += b"c" + struct.pack(">H", p.utf8(f"L{c};"))
            else:
                body += b"[" + struct.pack(">H", len(value))
                for s in value:
                    body += b"s" + struct.pack(">H", p.utf8(s))
    return struct.pack(">HI", p.utf8("RuntimeInvisibleAnnotations"), len(body)) + body


def mixin_class(name, targets, methods):
    """methods: [(name, [(annotation type, {key: values})]) or (name, anns, descriptor)]."""
    p = Pool()
    this, sup = p.cls(name), p.cls("java/lang/Object")
    body = struct.pack(">H", 0)  # no fields
    body += struct.pack(">H", len(methods))
    for mname, anns, *desc in methods:
        body += struct.pack(">HHH", 0x0001, p.utf8(mname), p.utf8(desc[0] if desc else "()V"))
        body += struct.pack(">H", 1) + annotations(p, anns)
    body += struct.pack(">H", 1) + annotations(p, [(MIXIN, {"value": tuple(targets)})])
    head = b"\xca\xfe\xba\xbe" + struct.pack(">HH", 0, 65) + p.bytes()
    head += struct.pack(">HHH", 0x0021, this, sup) + struct.pack(">H", 0)
    return head + body


def test_parse_mixin_reads_targets_and_injectors():
    data = mixin_class("mod/RendererMixin", ["net/minecraft/LevelRenderer"], [
        ("onRender", [(INJECT, {"method": ["render"]})]),
        ("redirectCull", [(REDIRECT, {"method": ["renderLevel(J)V"]})]),
        ("isVisible", [(OVERWRITE, {})]),
    ])
    m = parse_mixin(data)
    assert m.targets == ["net/minecraft/LevelRenderer"]
    assert [(i.kind, i.methods) for i in m.injections] == [
        ("Inject", ["render"]), ("Redirect", ["renderLevel(J)V"]), ("Overwrite", ["isVisible()V"])]


def test_non_mixin_class_is_none():
    from tests.test_memberlink import class_file
    assert parse_mixin(class_file("mod/Plain")) is None


def test_selector_name_strips_owner_and_descriptor():
    assert selector_name("render") == "render"
    assert selector_name("tick()V") == "tick"
    assert selector_name("Lnet/minecraft/Level;tick(J)V") == "tick"


def test_jar_profile_groups_by_target():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mod/A.class", mixin_class("mod/A", ["net/minecraft/Entity"], [
            ("a", [(INJECT, {"method": ["tick"]})])]))
        z.writestr("mod/B.class", mixin_class("mod/B", ["net/minecraft/Entity"], [
            ("b", [(REDIRECT, {"method": ["tick()V"]})])]))
    profile = jar_profile(buf.getvalue())
    assert profile["net/minecraft/Entity"]["mixins"] == 2
    assert profile["net/minecraft/Entity"]["methods"] == {"tick": ["Inject", "Redirect"]}


# ---------------------------------------------------------------------------
# check_mixin_targets: do a build's mixins still apply on another version?
# ---------------------------------------------------------------------------

from modkeel.mixinscan import check_mixin_targets  # noqa: E402
from modkeel.symbols import parse_proguard_mappings  # noqa: E402

CI = "Lorg/spongepowered/asm/mixin/injection/callback/CallbackInfo;"
PLAYER = "Lnet/minecraft/world/entity/player/Player;"
PROBLEM = "Lnet/minecraft/world/entity/player/Player$BedSleepingProblem;"

# Monsters in the Closet's case (1.21.10 -> 1.21.11): the lambda kept its name and took a
# Component instead of a BedSleepingProblem; Screen.render was renamed renderContent.
BUILT_FOR = parse_proguard_mappings("""\
net.minecraft.world.level.block.BedBlock -> a:
    1:1:void lambda$useWithoutItem$2(net.minecraft.world.entity.player.Player,\
net.minecraft.world.entity.player.Player$BedSleepingProblem) -> a
    2:2:void tick() -> b
net.minecraft.client.gui.Entry -> b:
    1:1:void render(int) -> a
    2:2:void tooltip() -> b
""".replace("\\\n", ""), "1.21.10")
TARGET = parse_proguard_mappings("""\
net.minecraft.world.level.block.BedBlock -> a:
    1:1:void lambda$useWithoutItem$2(net.minecraft.world.entity.player.Player,\
net.minecraft.network.chat.Component) -> a
    2:2:void tick() -> b
net.minecraft.client.gui.Entry -> b:
    1:1:void renderContent(int) -> a
""".replace("\\\n", ""), "1.21.11")


def mixin_jar(tmp_path, classes, default_require=None):
    jar = tmp_path / "mod.jar"
    with zipfile.ZipFile(jar, "w") as z:
        for name, data in classes.items():
            z.writestr(f"{name}.class", data)
        config = {"package": "mod.mixin", "mixins": [n.rsplit("/", 1)[-1] for n in classes]}
        if default_require is not None:
            config["injectors"] = {"defaultRequire": default_require}
        import json
        z.writestr("mod.mixins.json", json.dumps(config))
    return jar


def check(tmp_path, methods, target="net/minecraft/world/level/block/BedBlock", **kw):
    jar = mixin_jar(tmp_path, {"mod/mixin/M": mixin_class("mod/mixin/M", [target], methods)},
                    **kw)
    return check_mixin_targets(jar, TARGET, BUILT_FOR)


class TestCheckMixinTargets:
    def test_inject_handler_no_longer_matches_like_monsters_in_the_closet(self, tmp_path):
        report = check(tmp_path, [("thingy", [(INJECT, {"method": ["lambda$useWithoutItem$2"]})],
                                   f"({PLAYER}{PROBLEM}{CI})V")])
        assert report.fatal == ["M -> BedBlock.lambda$useWithoutItem$2: parameters changed, "
                                "@Inject handler no longer matches"]

    def test_handler_without_target_parameters_is_fine(self, tmp_path):
        report = check(tmp_path, [("h", [(INJECT, {"method": ["lambda$useWithoutItem$2"]})],
                                   f"({CI})V")])
        assert report.checked == 1 and not report.fatal

    def test_unchanged_target_is_fine(self, tmp_path):
        report = check(tmp_path, [("h", [(INJECT, {"method": ["tick"]})], f"({CI})V")])
        assert (report.checked, report.fatal, report.warnings) == (1, [], [])

    def test_vanished_target_is_fatal_when_required(self, tmp_path):
        methods = [("h", [(INJECT, {"method": ["render"]})], f"(I{CI})V")]
        report = check(tmp_path, methods, target="net/minecraft/client/gui/Entry",
                       default_require=1)
        assert report.fatal == ["M -> Entry.render: target method gone (Inject)"]

    def test_vanished_target_is_a_warning_with_require_zero(self, tmp_path):
        methods = [("h", [(INJECT, {"method": ["render"]})], f"(I{CI})V")]
        report = check(tmp_path, methods, target="net/minecraft/client/gui/Entry")
        assert report.fatal == [] and report.warnings == [
            "M -> Entry.render: target method gone (Inject)"]

    def test_annotation_require_beats_the_config(self, tmp_path):
        methods = [("h", [(INJECT, {"method": ["render"], "require": 1})], f"(I{CI})V")]
        report = check(tmp_path, methods, target="net/minecraft/client/gui/Entry",
                       default_require=0)
        assert len(report.fatal) == 1

    def test_one_of_several_selectors_found_is_enough(self, tmp_path):
        methods = [("h", [(INJECT, {"method": ["render", "renderContent"]})], f"({CI})V")]
        report = check(tmp_path, methods, target="net/minecraft/client/gui/Entry",
                       default_require=1)
        assert report.fatal == [] and report.warnings == []

    def test_overwrite_of_a_vanished_method_is_fatal(self, tmp_path):
        report = check(tmp_path, [("tooltip", [(OVERWRITE, {})], "()V")],
                       target="net/minecraft/client/gui/Entry")
        assert report.fatal == ["M -> Entry.tooltip: target method gone (Overwrite)"]

    def test_what_did_not_resolve_in_the_build_version_is_skipped(self, tmp_path):
        """Loader patches, intermediary names, other mods' classes: unknown, not broken."""
        methods = [("h", [(INJECT, {"method": ["neoforgeAddedMethod"]})], f"({CI})V"),
                   ("w", [(INJECT, {"method": ["tick*"]})], f"({CI})V")]
        assert check(tmp_path, methods, default_require=1).checked == 0
        unknown = check(tmp_path, methods, target="net/minecraft/class_2244")
        assert (unknown.checked, unknown.fatal) == (0, [])
