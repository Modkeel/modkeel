"""Tests for build helpers: Gradle resolution, task narrowing, jar selection, error lines."""

import json
import os
import zipfile
from pathlib import Path

from modkeel.build import (build_tasks, key_error_lines, loader_subproject, resolve_gradle,
                            select_main_jar)
from modkeel.version import is_version_in_fabric_range

SCRIPT = "gradlew.bat" if os.name == "nt" else "gradlew"


def _fabric_jar(path: Path, mc_range: str, pad: int = 20_000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("fabric.mod.json", json.dumps({"id": "m", "version": "1",
                                                  "depends": {"minecraft": mc_range}}))
        z.writestr("pad.bin", os.urandom(pad))
    return path


def test_wildcard_fabric_range_matches_any_version():
    assert is_version_in_fabric_range("26.3", "*")
    assert not is_version_in_fabric_range("26.3", "~26.2")


def test_resolve_gradle_prefers_root_wrapper(tmp_path):
    (tmp_path / SCRIPT).write_text("")
    assert resolve_gradle(tmp_path) == (tmp_path, tmp_path / SCRIPT)


def test_resolve_gradle_uses_loader_subfolder(tmp_path):
    for name in ("Fabric", "NeoForge"):
        (tmp_path / name).mkdir()
        (tmp_path / name / SCRIPT).write_text("")
    assert resolve_gradle(tmp_path, "neoforge")[0] == tmp_path / "NeoForge"
    assert resolve_gradle(tmp_path)[0] == tmp_path / "Fabric"


def test_resolve_gradle_none_without_any_wrapper(tmp_path):
    (tmp_path / "src").mkdir()
    assert resolve_gradle(tmp_path) == (None, None)


def test_build_tasks_narrows_stonecutter_to_target(tmp_path):
    (tmp_path / "stonecutter.gradle.kts").write_text("")
    for v in ("26.2", "26.3", "26.3-neoforge", "26.30"):
        (tmp_path / "versions" / v).mkdir(parents=True)
    assert build_tasks(tmp_path, "26.3") == [":26.3:build", ":26.3-neoforge:build"]
    assert build_tasks(tmp_path, "27.1") == ["build"]
    assert build_tasks(tmp_path, None) == ["build"]


def test_build_tasks_plain_project(tmp_path):
    (tmp_path / "versions" / "26.3").mkdir(parents=True)
    assert build_tasks(tmp_path, "26.3") == ["build"]


def test_build_tasks_loader_subproject(tmp_path):
    (tmp_path / "Fabric").mkdir()
    (tmp_path / "Fabric" / "build.gradle").write_text("")
    assert loader_subproject(tmp_path, "fabric") == ":Fabric"
    assert loader_subproject(tmp_path, "neoforge") is None
    assert build_tasks(tmp_path, "26.3", "fabric") == [":Fabric:build"]


def test_select_main_jar_prefers_target_version(tmp_path):
    old = _fabric_jar(tmp_path / "a" / "old.jar", "~26.2", pad=80_000)
    new = _fabric_jar(tmp_path / "b" / "new.jar", "~26.3")
    assert select_main_jar([old, new], "26.3") == new
    assert select_main_jar([old, new]) == old
    assert select_main_jar([old], "26.3") == old


def test_key_error_lines_extracts_causes():
    out = ("> Task :compileJava\n\x1b[31mCaused by: java.io.IOException: boom\x1b[0m\n"
           "Foo.java:3: error: cannot find symbol\nFoo.java:3: error: cannot find symbol\n"
           "BUILD FAILED\n")
    assert key_error_lines(out) == ["Caused by: java.io.IOException: boom",
                                    "Foo.java:3: error: cannot find symbol"]


def test_key_error_lines_takes_gradle_cause():
    out = "* What went wrong:\nCould not resolve all files for configuration ':compileClasspath'.\n"
    assert key_error_lines(out) == [
        "Could not resolve all files for configuration ':compileClasspath'."]
