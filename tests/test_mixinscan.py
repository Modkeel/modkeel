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
            if isinstance(value, tuple):  # class literals
                body += b"[" + struct.pack(">H", len(value))
                for c in value:
                    body += b"c" + struct.pack(">H", p.utf8(f"L{c};"))
            else:
                body += b"[" + struct.pack(">H", len(value))
                for s in value:
                    body += b"s" + struct.pack(">H", p.utf8(s))
    return struct.pack(">HI", p.utf8("RuntimeInvisibleAnnotations"), len(body)) + body


def mixin_class(name, targets, methods):
    """methods: [(name, [(annotation type, {key: values})])]."""
    p = Pool()
    this, sup = p.cls(name), p.cls("java/lang/Object")
    body = struct.pack(">H", 0)  # no fields
    body += struct.pack(">H", len(methods))
    for mname, anns in methods:
        body += struct.pack(">HHH", 0x0001, p.utf8(mname), p.utf8("()V"))
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
        ("Inject", ["render"]), ("Redirect", ["renderLevel(J)V"]), ("Overwrite", ["isVisible"])]


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
