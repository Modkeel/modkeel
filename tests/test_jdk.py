"""The JDK a build runs on (modkeel/jdk.py): the target's Java, found or downloaded."""

import hashlib
import io
import os
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from modkeel import jdk


def fake_jdk(root: Path, major: int, name: str = None) -> Path:
    home = root / (name or f"jdk-{major}")
    (home / "bin").mkdir(parents=True)
    (home / "bin" / f"javac{jdk.EXE}").write_text("")
    (home / "release").write_text(f'JAVA_VERSION="{major}.0.1"\n')
    return home


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(jdk, "MODKEEL_HOME", tmp_path / "home")
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setattr(jdk.shutil, "which", lambda name: None)


class TestJavaFor:
    @pytest.mark.parametrize("mc, major", [
        ("26.3", 25), ("26.1.2", 25), ("1.21.10", 21), ("1.20.5", 21), ("1.20.4", 17),
        ("1.18.2", 17), ("1.17.1", 16), ("1.16.5", 8), (None, None), ("snapshot", None),
    ])
    def test_the_java_each_release_runs_on(self, mc, major):
        assert jdk.java_for(mc) == major


class TestPick:
    def test_the_exact_major_beats_a_newer_one(self, tmp_path):
        homes = [fake_jdk(tmp_path, 25), fake_jdk(tmp_path, 21)]
        assert jdk.pick(21, homes) == homes[1]

    def test_else_the_nearest_newer_one_and_never_an_older_one(self, tmp_path):
        homes = [fake_jdk(tmp_path, 25), fake_jdk(tmp_path, 17)]
        assert jdk.pick(21, homes) == homes[0]
        assert jdk.pick(25, [homes[1]]) is None

    def test_sees_java_home_and_its_own_jdks(self, tmp_path, monkeypatch):
        own = fake_jdk(jdk.jdks_dir(), 25, "jdk-25.0.4+1")
        monkeypatch.setenv("JAVA_HOME", str(fake_jdk(tmp_path, 21)))
        assert {jdk.jdk_major(h) for h in jdk.installed()} == {21, 25}
        assert own in jdk.installed()


class TestBuildEnv:
    def test_runs_gradle_on_the_targets_java(self, tmp_path, monkeypatch):
        home = fake_jdk(jdk.jdks_dir(), 25)
        monkeypatch.setenv("JAVA_HOME", str(fake_jdk(tmp_path, 21)))
        env = jdk.build_env("26.3", events=lambda e: None)
        assert env["JAVA_HOME"] == str(home)
        assert env["PATH"].startswith(str(home / "bin") + os.pathsep)

    def test_unknown_target_keeps_the_machines_own(self):
        assert jdk.build_env(None) is None

    def test_downloads_the_missing_java_once_checked(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("jdk-25.0.4+1/bin/javac.exe", "")
            z.writestr("jdk-25.0.4+1/bin/javac", "")
            z.writestr("jdk-25.0.4+1/release", 'JAVA_VERSION="25.0.4"\n')
        data = buf.getvalue()
        package = {"name": "jdk.zip", "link": "https://x/jdk.zip", "size": len(data),
                   "checksum": hashlib.sha256(data).hexdigest()}

        def get(url, **kw):
            r = MagicMock()
            r.json.return_value = [{"binary": {"package": package}}]
            r.iter_content.return_value = [data]
            r.__enter__ = lambda s: r
            r.__exit__ = lambda *a: None
            return r

        messages = []
        with patch.object(jdk.requests, "get", get):
            env = jdk.build_env("26.3", events=messages.append)
        assert env["JAVA_HOME"] == str(jdk.jdks_dir() / "jdk-25.0.4+1")
        assert "Downloading Java 25" in messages[0].text
        # the next build finds it installed
        with patch.object(jdk.requests, "get", side_effect=AssertionError("no download")):
            assert jdk.build_env("26.3", events=messages.append)["JAVA_HOME"] == env["JAVA_HOME"]

    def test_a_download_that_does_not_match_its_checksum_is_not_used(self):
        package = {"name": "jdk.zip", "link": "https://x/jdk.zip", "size": 3,
                   "checksum": "0" * 64}

        def get(url, **kw):
            r = MagicMock()
            r.json.return_value = [{"binary": {"package": package}}]
            r.iter_content.return_value = [b"bad"]
            r.__enter__ = lambda s: r
            r.__exit__ = lambda *a: None
            return r

        messages = []
        with patch.object(jdk.requests, "get", get):
            assert jdk.build_env("26.3", events=messages.append) is None
        assert any("checksum" in m.text for m in messages)
        assert not list(jdk.jdks_dir().glob("jdk-*"))
