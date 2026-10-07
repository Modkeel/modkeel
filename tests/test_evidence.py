"""Tests for modkeel/evidence.py: each check's outcome, and how gather combines them.

The checks' collaborators (validate_jar, the symbol tables, DockerTester) are patched; the
real linkage logic is covered in test_linkage.py and the Docker runs in test_docker.py.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from modkeel.evidence import (
    EVIDENCE_ORDER,
    FAILED,
    NOT_RUN,
    PASSED,
    Outcome,
    Subject,
    check_linkage,
    check_metadata,
    check_mixins,
    check_server_boot,
    evidence_line,
    gather,
)
from modkeel.models import ModCompilerConfig


@pytest.fixture
def config(tmp_path):
    return ModCompilerConfig("1.21.10", "neoforge", "0", output_dir=str(tmp_path / "out"))


def subject(mc="1.21.10", built_for=None, deps=()):
    return Subject(Path("m.jar"), mc, built_for, "M", list(deps))


class TestMetadata:
    def test_passes(self, config):
        with patch("modkeel.evidence.validate_jar", return_value=(True, "m", "1", "ok")):
            assert check_metadata(subject(), config).status == PASSED

    def test_a_tiny_published_jar_is_not_suspicious(self, config, tmp_path):
        """Monsters in the Closet 1.0.3 is 10,238 bytes: real, under the build-output floor."""
        import json
        import zipfile
        jar = tmp_path / "tiny.jar"
        with zipfile.ZipFile(jar, "w") as z:
            z.writestr("fabric.mod.json", json.dumps(
                {"id": "t", "version": "1", "depends": {"minecraft": "1.21.10"}}))
        assert check_metadata(Subject(jar, "1.21.10"), config).status == PASSED

    def test_names_the_declared_range(self, config):
        refused = (False, "m", "1", "JAR declares incompatible MC version: [1.21.1]")
        with patch("modkeel.evidence.validate_jar", return_value=refused):
            out = check_metadata(subject(), config)
        assert (out.status, out.detail) == (FAILED, "its metadata only allows MC: [1.21.1]")


class TestLinkage:
    def run(self, config, report, mc="1.21.10", built_for=None, indexes=None):
        load = (indexes.get if indexes is not None else (lambda v: object()))
        with patch("modkeel.mappings.load_index", side_effect=load), \
                patch("modkeel.linkage.check_jar", return_value=report) as check:
            return check_linkage(subject(mc, built_for), config), check

    def test_no_symbol_table(self, config):
        out, _ = self.run(config, None, indexes={})
        assert (out.status, out.detail) == (NOT_RUN,
                                            "cannot verify: no symbol table for MC 1.21.10")

    def test_unreadable_names_are_not_run(self, config):
        out, _ = self.run(config, MagicMock(checked=False, skip_reason="intermediary names"))
        assert (out.status, out.detail) == (NOT_RUN, "cannot verify (intermediary names)")

    def test_missing_classes(self, config):
        report = MagicMock(checked=True, is_clean=False, missing_classes=[
            "net.minecraft.client.renderer.FogRenderer", "net/minecraft/world/level/Old",
            "net.minecraft.A", "net.minecraft.B"], vanished_members=[])
        out, _ = self.run(config, report)
        assert out.status == FAILED and out.data is report
        assert out.detail == ("4 Minecraft classes it uses don't exist in 1.21.10 "
                              "(FogRenderer, Old, A, ...)")

    def test_vanished_members_name_the_calls(self, config):
        props = "net.minecraft.world.level.block.state.BlockBehaviour$Properties"
        report = MagicMock(checked=True, is_clean=False, missing_classes=[], vanished_members=[
            f"method {props}.noCollission()L{props.replace('.', '/')};",
            "field net.minecraft.world.entity.Entity.level"])
        out, check = self.run(config, report, "1.21.9", "1.21.8",
                              indexes={"1.21.9": "target", "1.21.8": "built"})
        assert check.call_args.args[1:] == ("target", "built")
        assert out.detail == ("2 methods/fields it calls were removed or renamed after 1.21.8 "
                              "(BlockBehaviour$Properties.noCollission(), Entity.level)")

    def test_both_kinds_are_listed(self, config):
        report = MagicMock(checked=True, is_clean=False, missing_classes=["net.minecraft.X"],
                           vanished_members=["method net.minecraft.Y.z(I)V"])
        out, _ = self.run(config, report, "1.21.9", "1.21.8")
        assert out.detail == ("1 Minecraft classes it uses don't exist in 1.21.9 (X); "
                              "1 methods/fields it calls were removed or renamed after "
                              "1.21.8 (Y.z())")

    def test_built_for_mappings_missing_falls_back_to_classes(self, config):
        report = MagicMock(checked=True, is_clean=True, summary="ok")
        out, check = self.run(config, report, "1.21.9", "1.21.8", indexes={"1.21.9": "target"})
        assert out.status == PASSED
        assert check.call_args.args[1:] == ("target", None)

    def test_intermediary_jar_is_checked_again_in_its_names(self, config):
        loads = []

        def load(v, flavor="mojmap"):
            loads.append((v, flavor))
            return f"{flavor}-{v}"

        skipped = MagicMock(checked=False, scheme="intermediary")
        clean = MagicMock(checked=True, is_clean=True, summary="ok")
        with patch("modkeel.mappings.load_index", side_effect=load), \
                patch("modkeel.linkage.check_jar", side_effect=[skipped, clean]) as check:
            out = check_linkage(subject("1.21.4", "1.21.1"), config)
        assert out.status == PASSED
        assert check.call_args.args[1:] == ("intermediary-1.21.4", "intermediary-1.21.1")

    def test_clean(self, config):
        out, _ = self.run(config, MagicMock(checked=True, is_clean=True, summary="ok"))
        assert (out.status, out.detail) == (PASSED, "ok")


class TestMixins:
    def run(self, config, report=None, built_for="1.21.10", indexes=None, error=None,
            intermediary=False):
        from modkeel.mixinscan import MixinReport
        loads = []

        def load(v, flavor="mojmap"):
            loads.append(flavor)
            return indexes.get(v) if indexes is not None else MagicMock(flavor=flavor)

        scan = patch("modkeel.mixinscan.check_mixin_targets",
                     side_effect=error, return_value=report or MixinReport(checked=3))
        with patch("modkeel.mappings.load_index", side_effect=load), scan, \
                patch("modkeel.mixinscan.uses_intermediary", return_value=intermediary):
            self.loads = loads
            return check_mixins(subject("1.21.11", built_for), config)

    def test_needs_the_build_version(self, config):
        out = self.run(config, built_for=None)
        assert (out.status, out.detail) == (NOT_RUN, "needs the version the build targets")

    def test_no_symbol_table(self, config):
        assert self.run(config, indexes={"1.21.11": object()}).status == NOT_RUN

    def test_unreadable_jar(self, config):
        import zipfile
        out = self.run(config, error=zipfile.BadZipFile("not a zip"))
        assert out.status == NOT_RUN and "unreadable JAR" in out.detail

    def test_intermediary_jar_is_judged_in_intermediary_names(self, config):
        assert self.run(config, intermediary=True).status == PASSED
        assert self.loads == ["intermediary", "intermediary"]

    def test_intermediary_build_on_an_unobfuscated_target_is_not_run(self, config):
        indexes = {"1.21.11": MagicMock(flavor="unobfuscated"),
                   "1.21.10": MagicMock(flavor="intermediary")}
        out = self.run(config, indexes=indexes, intermediary=True)
        assert out.status == NOT_RUN and "name the game differently" in out.detail

    def test_fatal_injections_fail_and_are_named(self, config):
        from modkeel.mixinscan import MixinReport
        report = MixinReport(checked=6, fatal=["BedBlockMixin -> BedBlock.a: x",
                                               "B -> C.d: y", "E -> F.g: z"])
        out = self.run(config, report)
        assert out.status == FAILED
        assert out.detail == ("3 mixin injections would fail to apply on 1.21.11: "
                              "BedBlockMixin -> BedBlock.a: x; B -> C.d: y (+1 more)")

    def test_optional_injections_only_add_a_note(self, config):
        from modkeel.mixinscan import MixinReport
        out = self.run(config, MixinReport(checked=4, warnings=["A -> B.c: gone"]))
        assert (out.status, out.detail) == (
            PASSED, "4 mixin injections still apply; 1 optional ones lost their target")


class TestServerBoot:
    def run(self, config, available=True, passed=True, error=None):
        seen = {}

        def fake_test(self, results):
            seen["jars"] = [r.jar_path for r in results]
            results[0].docker_test_passed = passed
            results[0].docker_error = error

        with patch("modkeel.docker.DockerTester.check_docker_available",
                   return_value=available), \
                patch("modkeel.docker.DockerTester.test_mods_in_docker", fake_test):
            return check_server_boot(subject(deps=[Path("dep.jar")]), config), seen

    def test_boots_with_its_dependencies(self, config):
        out, seen = self.run(config)
        assert out.status == PASSED
        assert seen["jars"] == ["m.jar", "dep.jar"]

    def test_no_docker(self, config):
        out, _ = self.run(config, available=False)
        assert out.status == NOT_RUN and "needs Docker" in out.detail

    def test_client_only_is_not_run(self, config):
        out, _ = self.run(config, passed=None,
                          error="[CLIENT-ONLY] Mod uses client-side classes")
        assert out.status == NOT_RUN
        assert "inconclusive" in out.detail and "CLIENT-ONLY" in out.detail

    def test_crash(self, config):
        out, _ = self.run(config, passed=False, error="NoSuchMethodError")
        assert out.status == FAILED and "did not boot" in out.detail


class TestGather:
    """Cheapest first; stop at a failure or at a required check that could not run."""

    def run(self, config, outcomes, checks, required=None):
        calls = []

        def fake(name):
            def check(s, c):
                calls.append(name)
                return outcomes[name]
            return check

        with patch("modkeel.evidence.check_metadata", fake("metadata")), \
                patch("modkeel.evidence.check_linkage", fake("linkage")), \
                patch("modkeel.evidence.check_server_boot", fake("docker_server")):
            return gather(subject(), config, checks, required), calls

    def test_runs_cheapest_first_whatever_the_order_asked(self, config):
        outcomes = {n: Outcome(n, PASSED) for n in ("metadata", "linkage", "docker_server")}
        evidence, calls = self.run(config, outcomes, ["docker_server", "metadata", "linkage"])
        assert calls == ["metadata", "linkage", "docker_server"]
        assert evidence.ok and evidence.passed == calls

    def test_a_failure_stops_before_costlier_checks(self, config):
        outcomes = {"metadata": Outcome("metadata", FAILED, "range"),
                    "docker_server": Outcome("docker_server", PASSED)}
        evidence, calls = self.run(config, outcomes, ["metadata", "docker_server"])
        assert calls == ["metadata"]
        assert not evidence.ok and evidence.reason == "range"

    def test_required_check_that_cannot_run_rejects(self, config):
        outcomes = {"docker_server": Outcome("docker_server", NOT_RUN, "needs Docker")}
        evidence, _ = self.run(config, outcomes, ["docker_server"])
        assert not evidence.ok and evidence.reason == "needs Docker"

    def test_optional_check_that_cannot_run_is_only_reported(self, config):
        outcomes = {"linkage": Outcome("linkage", PASSED),
                    "docker_server": Outcome("docker_server", NOT_RUN, "needs Docker")}
        evidence, _ = self.run(config, outcomes, ["linkage", "docker_server"],
                               required=["linkage"])
        assert evidence.ok and evidence.passed == ["linkage"]
        assert evidence.outcomes[-1].detail == "needs Docker"

    def test_optional_failure_still_fails(self, config):
        """Not required means "may be unavailable", never "may crash"."""
        outcomes = {"docker_server": Outcome("docker_server", FAILED, "crashed")}
        evidence, _ = self.run(config, outcomes, ["docker_server"], required=[])
        assert not evidence.ok

    def test_check_without_an_implementation(self, config):
        evidence, _ = self.run(config, {}, ["client"], required=[])
        assert evidence.outcomes == [Outcome("client", NOT_RUN, "no client check available")]

    def test_order_is_by_cost(self):
        assert EVIDENCE_ORDER == ["metadata", "linkage", "mixins", "docker_server", "client"]


class TestEvidenceLine:
    def test_static_evidence_suggests_the_server_test(self):
        assert evidence_line(["metadata", "linkage"]) == (
            "metadata ✓ · linkage ✓ · server boot not run (--docker-test)")

    def test_server_boot_passed(self):
        assert evidence_line(["published", "docker_server"]) == (
            "official build for this version ✓ · server boot ✓")

    def test_docker_requested_but_not_passed_is_not_suggested_again(self):
        assert evidence_line(["built"], docker_requested=True) == "compiled for this version ✓"
