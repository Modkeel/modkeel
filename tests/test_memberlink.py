"""Tests for member-level linkage, on class files assembled byte by byte."""

import struct
import zipfile
from pathlib import Path

from modkeel.memberlink import (FOUND, MISSING, UNKNOWN, ClassDB, check_jar_members,
                                 member_refs, parse_class_info)


class Pool:
    def __init__(self):
        self.entries, self.index = [], {}

    def _add(self, key, raw):
        if key not in self.index:
            self.entries.append(raw)
            self.index[key] = len(self.entries)
        return self.index[key]

    def utf8(self, s):
        b = s.encode()
        return self._add(("u", s), b"\x01" + struct.pack(">H", len(b)) + b)

    def cls(self, name):
        return self._add(("c", name), b"\x07" + struct.pack(">H", self.utf8(name)))

    def nat(self, name, desc):
        return self._add(("n", name, desc),
                         b"\x0c" + struct.pack(">HH", self.utf8(name), self.utf8(desc)))

    def ref(self, tag, owner, name, desc):
        return self._add(("r", tag, owner, name, desc),
                         bytes([tag]) + struct.pack(">HH", self.cls(owner), self.nat(name, desc)))

    def bytes(self):
        return struct.pack(">H", len(self.entries) + 1) + b"".join(self.entries)


def class_file(name, super_name="java/lang/Object", interfaces=(), fields=(), methods=(),
               refs=(), mixin_target=None):
    p = Pool()
    this, sup = p.cls(name), p.cls(super_name) if super_name else 0
    ifaces = [p.cls(i) for i in interfaces]
    for kind, owner, mname, desc in refs:
        p.ref(9 if kind == "field" else 10, owner, mname, desc)

    def members(items):
        out = struct.pack(">H", len(items))
        for mname, desc in items:
            out += struct.pack(">HHHH", 0x0001, p.utf8(mname), p.utf8(desc), 0)
        return out

    body_fields, body_methods = members(fields), members(methods)
    attrs = b"\x00\x00"
    if mixin_target:
        # RuntimeInvisibleAnnotations: 1 annotation @Mixin(value = {Target.class})
        ann = struct.pack(">HHH", p.utf8("Lorg/spongepowered/asm/mixin/Mixin;"), 1,
                          p.utf8("value"))
        ann += b"[" + struct.pack(">H", 1) + b"c" + struct.pack(">H", p.utf8(f"L{mixin_target};"))
        payload = struct.pack(">H", 1) + ann
        attrs = struct.pack(">HHI", 1, p.utf8("RuntimeInvisibleAnnotations"), len(payload)) + payload
    head = b"\xca\xfe\xba\xbe" + struct.pack(">HH", 0, 65) + p.bytes()
    head += struct.pack(">HHH", 0x0021, this, sup)
    head += struct.pack(">H", len(ifaces)) + b"".join(struct.pack(">H", i) for i in ifaces)
    return head + body_fields + body_methods + attrs


def jar(path: Path, classes: dict) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in classes.items():
            z.writestr(name + ".class", data)
    return path


def game(tmp_path):
    return jar(tmp_path / "game.jar", {
        "net/minecraft/Base": class_file("net/minecraft/Base", methods=[("tick", "()V")],
                                         fields=[("level", "I")]),
        "net/minecraft/Api": class_file("net/minecraft/Api", super_name="java/lang/Object",
                                        methods=[("hook", "()V")]),
        "net/minecraft/Block": class_file("net/minecraft/Block", "net/minecraft/Base",
                                          interfaces=["net/minecraft/Api"],
                                          methods=[("place", "(I)Z")]),
    })


def test_parse_class_info_reads_hierarchy_and_members():
    info = parse_class_info(class_file("a/B", "a/A", ["a/I"], [("f", "I")], [("m", "()V")]))
    assert (info.name, info.super_name, info.interfaces) == ("a/B", "a/A", ["a/I"])
    assert info.fields == {("f", "I")} and info.methods == {("m", "()V")}


def test_member_refs_lists_field_and_method_refs():
    data = class_file("mod/X", refs=[("method", "net/minecraft/Block", "place", "(I)Z"),
                                     ("field", "net/minecraft/Base", "level", "I")])
    this, refs = member_refs(data)
    assert this == "mod/X"
    assert ("class", "net/minecraft/Block", "", "") in refs
    assert {r for r in refs if r[0] != "class"} == {("method", "net/minecraft/Block", "place", "(I)Z"),
                    ("field", "net/minecraft/Base", "level", "I")}


def test_resolve_walks_supers_and_interfaces(tmp_path):
    db = ClassDB()
    db.add_jar(game(tmp_path))
    assert db.resolve("method", "net/minecraft/Block", "tick", "()V") == FOUND
    assert db.resolve("method", "net/minecraft/Block", "hook", "()V") == FOUND
    assert db.resolve("field", "net/minecraft/Block", "level", "I") == FOUND
    assert db.resolve("method", "net/minecraft/Block", "toString", "()Ljava/lang/String;") == FOUND
    assert db.resolve("method", "net/minecraft/Block", "place", "(J)Z") == MISSING
    assert db.resolve("method", "net/minecraft/Gone", "x", "()V") == UNKNOWN


def test_check_jar_flags_missing_member_and_class(tmp_path):
    db = ClassDB()
    db.add_jar(game(tmp_path))
    mod = jar(tmp_path / "mod.jar", {"mod/X": class_file("mod/X", refs=[
        ("method", "net/minecraft/Block", "place", "(I)Z"),
        ("method", "net/minecraft/Block", "remove", "()V"),
        ("method", "net/minecraft/Removed", "x", "()V"),
        ("method", "java/util/List", "size", "()I")])})
    rep = check_jar_members(mod, db)
    assert rep.missing_members == ["method net/minecraft/Block.remove()V"]
    assert rep.missing_classes == ["net/minecraft/Removed"]
    assert rep.sites["net/minecraft/Removed"] == ["mod/X"]
    # 3 member refs + the 2 game classes named in the pool
    assert rep.refs_checked == 5


def test_mixin_members_count_as_added_to_target(tmp_path):
    db = ClassDB()
    db.add_jar(game(tmp_path))
    mod = jar(tmp_path / "mod.jar", {
        "mod/BlockMixin": class_file("mod/BlockMixin", methods=[("mod$extra", "()V")],
                                     mixin_target="net/minecraft/Block"),
        "mod/X": class_file("mod/X", refs=[("method", "net/minecraft/Block", "mod$extra",
                                            "()V")])})
    assert check_jar_members(mod, db).missing_members == []


def test_patched_loader_miss_on_unpatched_class_is_unverified(tmp_path):
    db = ClassDB()
    db.add_jar(game(tmp_path))
    mod = jar(tmp_path / "mod.jar", {"mod/X": class_file("mod/X", refs=[
        ("method", "net/minecraft/Block", "neoOnly", "()V")])})
    rep = check_jar_members(mod, db, patched_only={"net/minecraft/Base"})
    assert rep.missing_members == [] and rep.unverified == [
        "method net/minecraft/Block.neoOnly()V"]


def test_missing_supertype_is_a_missing_class(tmp_path):
    db = ClassDB()
    db.add_jar(game(tmp_path))
    mod = jar(tmp_path / "mod.jar", {"mod/Trigger": class_file(
        "mod/Trigger", interfaces=["net/minecraft/CriterionTrigger"])})
    assert check_jar_members(mod, db).missing_classes == ["net/minecraft/CriterionTrigger"]


def test_enum_field_resolves_through_jdk_super(tmp_path):
    db = ClassDB()
    jdk = jar(tmp_path / "jdk.jar", {"java/lang/Enum": class_file("java/lang/Enum")})
    db.add_jar(jdk, track_packages=False)
    db.add_jar(jar(tmp_path / "g.jar", {"net/minecraft/Type": class_file(
        "net/minecraft/Type", "java/lang/Enum", fields=[("KEYBOARD", "Lnet/minecraft/Type;")])}))
    assert db.resolve("field", "net/minecraft/Type", "KEYSYM", "Lnet/minecraft/Type;") == MISSING
    assert db.resolve("field", "net/minecraft/Type", "KEYBOARD", "Lnet/minecraft/Type;") == FOUND


def test_neoforge_interfaces_json_injects_interfaces(tmp_path):
    db = ClassDB()
    db.add_jar(game(tmp_path))
    mod = tmp_path / "mod.jar"
    with zipfile.ZipFile(mod, "w") as z:
        z.writestr("META-INF/interfaces.json", '{"net/minecraft/Block": ["mod/Provider"]}')
        z.writestr("mod/Provider.class", class_file("mod/Provider", methods=[("extra", "()V")]))
        z.writestr("mod/X.class", class_file("mod/X", refs=[
            ("method", "net/minecraft/Block", "extra", "()V")]))
    assert check_jar_members(mod, db).missing_members == []
