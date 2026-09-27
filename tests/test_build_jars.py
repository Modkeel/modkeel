"""find_output_jars: JAR discovery across root and subproject build/libs."""

import zipfile

from modforge.build import find_output_jars


def _jar(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name in entries:
            z.writestr(name, "x")
    return path


def test_subproject_mod_jars_found(tmp_path):
    neo = _jar(tmp_path / "neoforge/build/libs/mod-neoforge.jar", ["META-INF/neoforge.mods.toml"])
    fab = _jar(tmp_path / "fabric/build/libs/mod-fabric.jar", ["fabric.mod.json"])
    _jar(tmp_path / "common/build/libs/common.jar", ["a/B.class"])
    _jar(tmp_path / "buildSrc/build/libs/buildSrc.jar", ["fabric.mod.json"])
    _jar(tmp_path / "neoforge/build/libs/mod-neoforge-sources.jar", ["META-INF/neoforge.mods.toml"])
    assert set(find_output_jars(tmp_path)) == {neo, fab}


def test_single_module_without_metadata_still_found(tmp_path):
    jar = _jar(tmp_path / "build/libs/plain.jar", ["a/B.class"])
    assert find_output_jars(tmp_path) == [jar]


def test_nothing_built(tmp_path):
    assert find_output_jars(tmp_path) == []
