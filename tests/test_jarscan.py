"""Tests for the JAR scanner: what a mod can do outside the game, read from its bytecode."""

import io
import struct
import zipfile

from modkeel.jarscan import HIGH, MEDIUM, read_class, scan_jar


def class_file(name, methods=(), strings=()):
    """A class file named `name` whose pool references `methods` ((owner, name) pairs) and
    holds `strings` as String constants."""
    entries = []

    def add(tag, payload):
        entries.append(bytes([tag]) + payload)
        return len(entries)

    def utf8(text):
        raw = text.encode("utf-8")
        return add(1, struct.pack(">H", len(raw)) + raw)

    def cls(internal):
        return add(7, struct.pack(">H", utf8(internal)))

    this = cls(name)
    for owner, method in methods:
        nat = add(12, struct.pack(">HH", utf8(method), utf8("()V")))
        add(10, struct.pack(">HH", cls(owner), nat))
    for s in strings:
        add(8, struct.pack(">H", utf8(s)))
    return (b"\xca\xfe\xba\xbe" + struct.pack(">HH", 0, 65) + struct.pack(">H", len(entries) + 1)
            + b"".join(entries) + struct.pack(">HHH", 0x21, this, 0))


def jar_bytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def write_jar(tmp_path, files, name="mod.jar"):
    path = tmp_path / name
    path.write_bytes(jar_bytes(files))
    return path


NET = ("java/net/URL", "openConnection")


def kinds(report):
    return {(f.level, f.kind) for f in report.findings}


class TestReadClass:
    def test_reads_name_methods_strings(self):
        use = read_class(class_file("a/B", [NET], ["hello"]))
        assert use.name == "a/B"
        assert NET in use.methods
        assert "hello" in use.strings
        assert "java/net/URL" in use.classes

    def test_not_a_class(self):
        assert read_class(b"PK not a class") is None


class TestScanJar:
    def test_plain_mod_is_clean(self, tmp_path):
        jar = write_jar(tmp_path, {"a/Mod.class": class_file("a/Mod", [NET], ["modid"])})
        report = scan_jar(jar)
        assert report.classes == 1
        assert report.level is None

    def test_known_family_class(self, tmp_path):
        jar = write_jar(tmp_path, {"x.class": class_file("dev/neko/nekoclient/Client")})
        assert (HIGH, "known-malware") in kinds(scan_jar(jar))

    def test_known_server(self, tmp_path):
        strings = ["http://85.217.144.130/x"]
        jar = write_jar(tmp_path, {"a/A.class": class_file("a/A", strings=strings)})
        assert scan_jar(jar).level == HIGH

    def test_webhook(self, tmp_path):
        url = "https://discord.com/api/webhooks/1/abc"
        jar = write_jar(tmp_path, {"a/A.class": class_file("a/A", strings=[url])})
        assert (HIGH, "exfiltration") in kinds(scan_jar(jar))

    def test_credentials_with_network_is_high(self, tmp_path):
        jar = write_jar(tmp_path, {
            "a/A.class": class_file("a/A", strings=["\\Local Storage\\leveldb"]),
            "a/B.class": class_file("a/B", [NET])})
        assert (HIGH, "credentials") in kinds(scan_jar(jar))

    def test_credentials_without_network_is_medium(self, tmp_path):
        jar = write_jar(tmp_path, {"a/A.class": class_file("a/A", strings=["/Login Data"])})
        assert kinds(scan_jar(jar)) == {(MEDIUM, "credentials")}

    def test_remote_code(self, tmp_path):
        jar = write_jar(tmp_path, {"a/A.class": class_file(
            "a/A", [NET, ("java/lang/ClassLoader", "defineClass")])})
        assert kinds(scan_jar(jar)) == {(MEDIUM, "remote-code")}

    def test_define_without_network_is_clean(self, tmp_path):
        # mixin and library loaders define classes from bytes they already have
        jar = write_jar(tmp_path, {"a/A.class": class_file(
            "a/A", [("java/lang/ClassLoader", "defineClass")])})
        assert scan_jar(jar).level is None

    def test_runs_programs(self, tmp_path):
        jar = write_jar(tmp_path, {"a/A.class": class_file(
            "a/A", [("java/lang/ProcessBuilder", "start")])})
        assert kinds(scan_jar(jar)) == {(MEDIUM, "runs-programs")}

    def test_hidden_code(self, tmp_path):
        jar = write_jar(tmp_path, {"a/A.class": class_file(
            "a/A", [("java/util/Base64$Decoder", "decode"),
                    ("java/lang/ClassLoader", "defineClass")],
            ["QUFB" * 1200])})
        assert (MEDIUM, "hidden-code") in kinds(scan_jar(jar))

    def test_nested_jar(self, tmp_path):
        inner = jar_bytes({"x.class": class_file("dev/neko/nekoinjector/Loader")})
        outer = write_jar(tmp_path, {"a/Mod.class": class_file("a/Mod"),
                                     "META-INF/jars/lib.jar": inner})
        report = scan_jar(outer)
        assert report.classes == 2
        assert report.level == HIGH

    def test_jar_outside_nested_dirs_not_opened(self, tmp_path):
        inner = jar_bytes({"x.class": class_file("dev/neko/nekoinjector/Loader")})
        outer = write_jar(tmp_path, {"assets/lib.jar": inner})
        assert scan_jar(outer).level is None

    def test_not_a_zip(self, tmp_path):
        bad = tmp_path / "bad.jar"
        bad.write_bytes(b"nope")
        report = scan_jar(bad)
        assert report.error and report.level is None

    def test_finding_line(self, tmp_path):
        jar = write_jar(tmp_path, {"a/A.class": class_file(
            "a/A", [("java/lang/Runtime", "exec")])})
        assert scan_jar(jar).findings[0].line() == "medium runs-programs in a/A: Runtime.exec"
