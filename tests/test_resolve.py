"""Tests for the resolver's source layer: modkeel/resolve.py and modkeel/sources.py.

The Resolver is tested with fake strategies (order, trail, find-only runs, attempt limits);
the real strategies with their Modrinth/linkage collaborators patched; project identity
(find_project) with canned Modrinth search hits.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from modkeel.models import ModCompilerConfig
from modkeel.modrinth import ModrinthClient
from modkeel.resolve import (
    Candidate,
    Delivered,
    Found,
    ModRef,
    Rejected,
    ResolveContext,
    Resolver,
    SourceStrategy,
)
from modkeel.sources import (
    SOURCE_ORDER,
    ForkSource,
    OfficialSource,
    OlderOfficialSource,
    RelaxedOfficialSource,
    _family,
    _linkage_rejection,
    default_strategies,
    in_source_order,
    possible_ports,
    possible_ports_note,
)


class FakeSource(SourceStrategy):
    """Finds the given candidates; delivers the ones named in `works`."""

    def __init__(self, name, candidates=(), works=(), note="nothing here"):
        self.name = name
        self.candidates = list(candidates)
        self.works = set(works)
        self.note = note
        self.found_calls = 0
        self.delivered = []

    def find(self, mod, ctx):
        self.found_calls += 1
        return Found([Candidate(c) for c in self.candidates], note=self.note)

    def deliver(self, candidate, mod, ctx):
        self.delivered.append(candidate.label)
        if candidate.label in self.works:
            return Delivered(jar_path=Path(candidate.label), mod_name="m", mod_version="1")
        return Rejected("broken")


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    # downloads are cached under MODKEEL_HOME: keep them in this test's tmp dir
    monkeypatch.setattr("modkeel.sources.MODKEEL_HOME", tmp_path / "home")
    config = ModCompilerConfig("1.21.10", "neoforge", "0", output_dir=str(tmp_path / "out"))
    return ResolveContext(config=config, modrinth=MagicMock(spec=ModrinthClient))


MOD = ModRef(query="Create", project={"project_id": "p", "slug": "create", "title": "Create"})


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

class TestResolver:
    def test_stops_at_first_delivered_and_records_the_path(self, ctx):
        a = FakeSource("a")
        b = FakeSource("b", ["b1", "b2"], works={"b2"})
        c = FakeSource("c", ["c1"], works={"c1"})
        res = Resolver([a, b, c]).resolve(MOD, ctx)
        assert res.delivered.jar_path == Path("b2")
        assert [(s.strategy, s.ok, s.detail) for s in res.trail] == [
            ("a", False, "nothing here"),
            ("b", False, "b1: broken"),
            ("b", True, "b2"),
        ]
        assert c.found_calls == 0

    def test_nothing_delivered(self, ctx):
        res = Resolver([FakeSource("a"), FakeSource("b", ["b1"])]).resolve(MOD, ctx)
        assert res.delivered is None
        assert [s.ok for s in res.trail] == [False, False]

    def test_max_attempts(self, ctx):
        b = FakeSource("b", ["b1", "b2", "b3"], works={"b3"})
        b.max_attempts = 2
        res = Resolver([b]).resolve(MOD, ctx)
        assert res.delivered is None and b.delivered == ["b1", "b2"]

    def test_find_only_leaves_candidates_pending(self, ctx):
        a = FakeSource("a")
        b = FakeSource("b", ["b1"], works={"b1"}, note="1 of 3 passed")
        res = Resolver([a, b]).resolve(MOD, ctx, deliver=False)
        assert res.delivered is None and b.delivered == []
        assert res.pending_strategy == "b" and res.pending_note == "1 of 3 passed"
        assert [c.label for c in res.pending_candidates] == ["b1"]

    def test_deliver_pending_reuses_the_lookup_then_goes_on(self, ctx):
        b = FakeSource("b", ["b1"])                   # pending, fails on delivery
        c = FakeSource("c", ["c1"], works={"c1"})
        resolver = Resolver([b, c])
        res = resolver.resolve(MOD, ctx, deliver=False)
        res = resolver.deliver_pending(res, MOD, ctx)
        assert b.found_calls == 1                      # not searched again
        assert res.delivered.jar_path == Path("c1")
        assert res.pending is None

    def test_default_order(self):
        assert SOURCE_ORDER == ["official", "official_source", "older_official", "fork",
                                "relaxed_official"]
        # get/search know no repo branches: official_source is skipped, order kept
        assert [s.name for s in default_strategies()] == [
            "official", "older_official", "fork", "relaxed_official"]

    def test_unknown_strategy_name_is_a_bug(self):
        with pytest.raises(KeyError):
            in_source_order([FakeSource("nope")])


# ---------------------------------------------------------------------------
# official
# ---------------------------------------------------------------------------

def version(number, mcs, filename="m.jar"):
    return {"version_number": number, "version_type": "release", "game_versions": mcs,
            "files": [{"primary": True, "url": f"https://cdn/{filename}",
                       "filename": filename}], "dependencies": []}


class TestOfficial:
    def test_not_identified(self, ctx):
        found = OfficialSource().find(ModRef(query="x"), ctx)
        assert not found.candidates and found.note == "Not found on Modrinth"

    def test_identification_failed(self, ctx):
        found = OfficialSource().find(ModRef(query="x", lookup_error="HTTP 503"), ctx)
        assert found.note == "Modrinth unavailable (HTTP 503)"

    def test_no_build_for_target(self, ctx):
        ctx.modrinth.project_versions.return_value = []
        found = OfficialSource().find(MOD, ctx)
        assert found.note == "Create has no NeoForge build for MC 1.21.10"
        ctx.modrinth.project_versions.assert_called_once_with("p", "neoforge", "1.21.10")

    def test_prefers_release(self, ctx):
        beta = {**version("2-beta", ["1.21.10"]), "version_type": "beta"}
        ctx.modrinth.project_versions.return_value = [beta, version("1", ["1.21.10"])]
        found = OfficialSource().find(MOD, ctx)
        assert found.candidates[0].data["version"]["version_number"] == "1"


# ---------------------------------------------------------------------------
# older_official
# ---------------------------------------------------------------------------

class TestOlderOfficial:
    def test_family(self):
        assert _family("1.21.10") == "1.21" and _family("26.2") == "26"

    def test_nearest_older_builds_of_the_same_line(self, ctx):
        ctx.modrinth.project_versions.return_value = [
            version("7", ["1.21.11"]),             # newer than target: not a fallback
            version("6c", ["1.21.9"]),
            version("6b", ["1.21.8", "1.21.9"]),   # same newest MC as 6c: 6c wins (newer)
            version("6a", ["1.21.7"]),
            version("5", ["1.21.5"]),
            version("4", ["1.21.4"]),              # beyond MAX_VERSIONS_BACK
            version("3", ["1.20.1"]),              # other line
            version("snap", ["25w31a"]),           # snapshot: not comparable
        ]
        found = OlderOfficialSource().find(MOD, ctx)
        assert [c.label for c in found.candidates] == [
            "6c for MC 1.21.9", "6a for MC 1.21.7", "5 for MC 1.21.5",
        ]
        ctx.modrinth.project_versions.assert_called_once_with("p", "neoforge")

    def test_build_listing_the_target_is_left_to_official(self, ctx):
        ctx.modrinth.project_versions.return_value = [version("6", ["1.21.9", "1.21.10"])]
        assert not OlderOfficialSource().find(MOD, ctx).candidates

    def test_year_based_versions(self, ctx):
        ctx.config.mc_version = "26.2"
        ctx.modrinth.project_versions.return_value = [
            version("b", ["26.1.2"]), version("a", ["1.21.11"])]
        found = OlderOfficialSource().find(MOD, ctx)
        assert [c.label for c in found.candidates] == ["b for MC 26.1.2"]

    def test_nothing_older(self, ctx):
        ctx.modrinth.project_versions.return_value = []
        found = OlderOfficialSource().find(MOD, ctx)
        assert found.note == "no older NeoForge builds in the 1.21 line"

    def _deliver(self, ctx, valid=(True, "m", "1", "ok"), linkage=None):
        cand = Candidate("1 for MC 1.21.9",
                         {"version": version("1", ["1.21.9"]), "built_for": "1.21.9"})
        ctx.modrinth.download_modrinth_deps.return_value = []
        with patch("modkeel.sources._download",
                   lambda url, dest: dest.write_bytes(b"PK")), \
                patch("modkeel.sources.validate_jar", return_value=valid), \
                patch("modkeel.sources._linkage_rejection", return_value=linkage) as check:
            out = OlderOfficialSource().deliver(cand, MOD, ctx)
        if valid[0]:
            # members are checked against the version the build targets
            assert check.call_args.args[1:] == (ctx.mc_version, "1.21.9")
        return out

    def test_metadata_must_allow_the_target(self, ctx):
        out = self._deliver(ctx, valid=(False, "m", "1",
                                        "JAR declares incompatible MC version: [1.21.9]"))
        assert isinstance(out, Rejected)
        assert out.reason == "its metadata only allows MC: [1.21.9]"
        assert not list(ctx.config.output_dir.iterdir())

    def test_linkage_must_pass(self, ctx):
        out = self._deliver(ctx, linkage=Rejected("3 Minecraft classes it uses don't exist"))
        assert isinstance(out, Rejected) and "3 Minecraft classes" in out.reason
        assert not list(ctx.config.output_dir.iterdir())

    def test_delivered_with_evidence_and_caveat(self, ctx):
        out = self._deliver(ctx)
        assert isinstance(out, Delivered)
        assert out.evidence == ["metadata", "linkage"]
        assert "Built for MC 1.21.9" in out.caveat
        assert "no method or field it calls was removed or renamed" in out.caveat
        assert (ctx.config.output_dir / "m.jar").exists()


class TestLinkageRejection:
    def test_no_symbol_table(self):
        with patch("modkeel.mappings.load_index", return_value=None):
            out = _linkage_rejection(Path("x.jar"), "1.21.10")
        assert out.reason == "cannot verify: no symbol table for MC 1.21.10"

    def test_inconclusive_is_a_rejection(self):
        report = MagicMock(checked=False, skip_reason="intermediary names")
        with patch("modkeel.mappings.load_index", return_value=object()), \
                patch("modkeel.linkage.check_jar", return_value=report):
            out = _linkage_rejection(Path("x.jar"), "1.21.10")
        assert out.reason == "cannot verify (intermediary names)"

    def test_missing_classes(self):
        report = MagicMock(checked=True, is_clean=False, missing_classes=[
            "net.minecraft.client.renderer.FogRenderer", "net/minecraft/world/level/Old",
            "net.minecraft.A", "net.minecraft.B"], vanished_members=[])
        with patch("modkeel.mappings.load_index", return_value=object()), \
                patch("modkeel.linkage.check_jar", return_value=report):
            out = _linkage_rejection(Path("x.jar"), "1.21.10")
        assert out.reason == ("4 Minecraft classes it uses don't exist in 1.21.10 "
                              "(FogRenderer, Old, A, ...)")

    def test_vanished_members_name_the_calls(self):
        props = "net.minecraft.world.level.block.state.BlockBehaviour$Properties"
        report = MagicMock(checked=True, is_clean=False, missing_classes=[], vanished_members=[
            f"method {props}.noCollission()L{props.replace('.', '/')};",
            "field net.minecraft.world.entity.Entity.level"])
        indexes = {"1.21.9": "target", "1.21.8": "built"}
        with patch("modkeel.mappings.load_index", side_effect=indexes.get), \
                patch("modkeel.linkage.check_jar", return_value=report) as check:
            out = _linkage_rejection(Path("x.jar"), "1.21.9", "1.21.8")
        assert check.call_args.args[1:] == ("target", "built")
        assert out.reason == ("2 methods/fields it calls were removed or renamed after 1.21.8 "
                              "(BlockBehaviour$Properties.noCollission(), Entity.level)")

    def test_both_kinds_are_listed(self):
        report = MagicMock(checked=True, is_clean=False, missing_classes=["net.minecraft.X"],
                           vanished_members=["method net.minecraft.Y.z(I)V"])
        with patch("modkeel.mappings.load_index", return_value=object()), \
                patch("modkeel.linkage.check_jar", return_value=report):
            out = _linkage_rejection(Path("x.jar"), "1.21.9", "1.21.8")
        assert out.reason == ("1 Minecraft classes it uses don't exist in 1.21.9 (X); "
                              "1 methods/fields it calls were removed or renamed after "
                              "1.21.8 (Y.z())")

    def test_built_for_mappings_missing_falls_back_to_classes(self):
        report = MagicMock(checked=True, is_clean=True, summary="ok")
        with patch("modkeel.mappings.load_index",
                   side_effect=lambda v: "target" if v == "1.21.9" else None), \
                patch("modkeel.linkage.check_jar", return_value=report) as check:
            assert _linkage_rejection(Path("x.jar"), "1.21.9", "1.21.8") is None
        assert check.call_args.args[1:] == ("target", None)

    def test_clean(self):
        report = MagicMock(checked=True, is_clean=True, summary="ok")
        with patch("modkeel.mappings.load_index", return_value=object()), \
                patch("modkeel.linkage.check_jar", return_value=report):
            assert _linkage_rejection(Path("x.jar"), "1.21.10") is None


# ---------------------------------------------------------------------------
# fork
# ---------------------------------------------------------------------------

class TestFork:
    def test_needs_a_token(self, ctx):
        found = ForkSource().find(MOD, ctx)
        assert not found.candidates and "modkeel token --set" in found.note

    def test_refused_search_is_not_reported_as_no_forks(self, ctx):
        from modkeel.github import GitHubClient

        def refused(self, owner, repo, cross):
            self.search_denied = 6
            return []

        ctx.github_token = lambda: "tok"
        with patch.object(GitHubClient, "search_compatible_repos", refused):
            found = ForkSource().find(MOD, ctx)
        assert found.note == ("GitHub refused 6 of the searches (rate limit or no access): "
                              "forks unknown")

    def test_needs_a_loader_version_to_compile(self, ctx):
        cand = Candidate("alice/create branch port", {"fork": {}, "token": "t"})
        out = ForkSource().deliver(cand, MOD, ctx)
        assert out.reason == "-lv LOADER_VERSION is required to compile"


# ---------------------------------------------------------------------------
# project identity
# ---------------------------------------------------------------------------

def hit(slug, title, downloads=0):
    return {"slug": slug, "title": title, "downloads": downloads, "project_id": slug}


def find(query, hits, status=200):
    client = ModrinthClient(ModCompilerConfig("1.21.10", "neoforge", "0"))
    resp = MagicMock(status_code=status)
    resp.json.return_value = {"hits": hits}
    with patch("modkeel.modrinth.requests.get", return_value=resp) as get:
        project, others = client.find_project(query)
    return project, others, client, get


class TestFindProject:
    def test_exact_slug_beats_more_popular_addon(self):
        hits = [hit("create-tfmg", "Create: The Factory Must Grow", 9),
                hit("create", "Create", 5)]
        project, others, _, get = find("Create", hits)
        assert project["slug"] == "create"
        assert [o["slug"] for o in others] == ["create-tfmg"]
        # identity search is not filtered by version or loader
        assert get.call_args.kwargs["params"]["facets"] == '[["project_type:mod"]]'

    def test_alias_in_parentheses(self):
        project, *_ = find("YACL", [hit("yacl-x", "YetAnotherConfigLib (YACL)")])
        assert project["slug"] == "yacl-x"
        project, *_ = find("YetAnotherConfigLib", [hit("yacl-x", "YetAnotherConfigLib (YACL)")])
        assert project["slug"] == "yacl-x"

    def test_punctuation_and_case(self):
        project, *_ = find("forge config api port",
                           [hit("forge-config-api-port", "Forge Config API Port")])
        assert project["slug"] == "forge-config-api-port"

    def test_most_downloaded_exact_match(self):
        project, *_ = find("Create", [hit("create-fork", "Create", 1), hit("create", "Create", 9)])
        assert project["slug"] == "create"

    def test_addon_alone_is_not_the_mod(self):
        project, others, client, _ = find(
            "Create", [hit("create-tfmg", "Create: The Factory Must Grow")])
        assert project is None and len(others) == 1 and client.last_error is None

    def test_http_error(self):
        project, others, client, _ = find("Create", [], status=503)
        assert project is None and client.last_error == "HTTP 503"


class TestFindProjectByRepo:
    def client(self):
        return ModrinthClient(ModCompilerConfig("1.21.10", "neoforge", "0"))

    def by_repo(self, hits, sources):
        """find_project returns `hits`; Modrinth says project id -> source_url per `sources`."""
        client = self.client()

        def tag(hits_, source_repo, headers):
            out = []
            for h in hits_:
                src = sources.get(h["project_id"], "")
                if src.lower() == source_repo.lower():
                    out.append({**h, "_source_match": True})
                elif not src:
                    out.append(h)
            return out

        with patch.object(ModrinthClient, "find_project", return_value=(hits[0], hits[1:])), \
                patch.object(ModrinthClient, "_filter_hits_by_source", side_effect=tag):
            return client.find_project_by_repo("Engine-Room", "Flywheel")

    def test_only_a_project_built_from_that_repo(self):
        hits = [hit("flywheel", "Flywheel (Legacy)"), hit("flw-vanillin", "Vanillin")]
        project = self.by_repo(hits, {"flywheel": "Jozufozu/Flywheel",
                                      "flw-vanillin": "Engine-Room/Flywheel"})
        assert project["slug"] == "flw-vanillin"

    def test_named_like_the_repo_wins_among_several(self):
        hits = [hit("flw-vanillin", "Vanillin", 9), hit("flywheel-x", "Flywheel", 1)]
        project = self.by_repo(hits, {"flw-vanillin": "Engine-Room/Flywheel",
                                      "flywheel-x": "Engine-Room/Flywheel"})
        assert project["slug"] == "flywheel-x"

    def test_no_project_names_the_repo(self):
        hits = [hit("flywheel", "Flywheel (Legacy)")]
        assert self.by_repo(hits, {"flywheel": "Jozufozu/Flywheel"}) is None


# ---------------------------------------------------------------------------
# relaxed_official
# ---------------------------------------------------------------------------

class TestRelaxedOfficial:
    CAND = Candidate("1 for MC 1.21.9", {"version": version("1", ["1.21.9"], "mod.jar"),
                                          "built_for": "1.21.9"})

    def deliver(self, ctx, valid, linkage=None, relaxed=True, boot=None):
        from modkeel.relax import Relaxed
        ctx.modrinth.download_modrinth_deps.return_value = []
        downloads = []

        def fake_download(url, dest):
            downloads.append(url)
            dest.write_bytes(b"PK")

        def fake_relax(src, dest, built_for, target):
            if not relaxed:
                return None
            dest.write_bytes(b"PK relaxed")
            return Relaxed("META-INF/neoforge.mods.toml", "[1.21.9]", f"[1.21.9,{target}]")

        with patch("modkeel.sources._download", fake_download), \
                patch("modkeel.sources.validate_jar", side_effect=valid), \
                patch("modkeel.sources._linkage_rejection", return_value=linkage), \
                patch("modkeel.relax.relax_jar", fake_relax), \
                patch("modkeel.sources._server_boot_rejection", return_value=boot):
            out = RelaxedOfficialSource().deliver(self.CAND, MOD, ctx)
        return out, downloads

    def test_relaxed_and_marked(self, ctx):
        out, _ = self.deliver(ctx, valid=[(False, "m", "1", "range"), (True, "m", "1", "ok")])
        assert isinstance(out, Delivered)
        assert out.jar_path.name == "mod+modkeel-relaxed-mc1.21.10.jar"
        assert out.evidence == ["linkage", "metadata_relaxed", "docker_server"]
        assert "declared [1.21.9]" in out.caveat and "server booted" in out.caveat

    def test_not_delivered_unless_a_server_boots(self, ctx):
        """TorchMaster 21.8.2 relaxed to 1.21.9 passed linkage and crashed on boot."""
        out, _ = self.deliver(ctx, valid=[(False, "m", "1", "range"), (True, "m", "1", "ok")],
                              boot=Rejected("a server did not boot with it (NoSuchMethodError)"))
        assert isinstance(out, Rejected) and "did not boot" in out.reason
        assert not list(ctx.config.output_dir.glob("*.jar"))

    def test_metadata_already_allows_target(self, ctx):
        out, _ = self.deliver(ctx, valid=[(True, "m", "1", "ok")])
        assert isinstance(out, Rejected) and "older_official's case" in out.reason

    def test_linkage_must_pass(self, ctx):
        out, _ = self.deliver(ctx, valid=[(False, "m", "1", "range")],
                              linkage=Rejected("3 Minecraft classes it uses don't exist"))
        assert isinstance(out, Rejected) and "3 Minecraft classes" in out.reason
        assert not list(ctx.config.output_dir.glob("*.jar"))

    def test_nothing_to_rewrite(self, ctx):
        out, _ = self.deliver(ctx, valid=[(False, "m", "1", "range")], relaxed=False)
        assert out.reason == "no Minecraft range in its metadata to rewrite"

    def test_still_refused_after_rewrite_is_removed(self, ctx):
        out, _ = self.deliver(ctx, valid=[(False, "m", "1", "range"),
                                          (False, "m", "1", "other")])
        assert "still refused" in out.reason
        assert not list(ctx.config.output_dir.glob("*.jar"))

    def test_download_is_shared_with_older_official(self, ctx):
        """older_official rejects it on metadata, relaxed_official reuses the same file."""
        ctx.modrinth.download_modrinth_deps.return_value = []
        downloads = []

        def fake_download(url, dest):
            downloads.append(url)
            dest.write_bytes(b"PK")

        with patch("modkeel.sources._download", fake_download), \
                patch("modkeel.sources.validate_jar",
                      return_value=(False, "m", "1", "JAR declares incompatible MC version: x")):
            OlderOfficialSource().deliver(self.CAND, MOD, ctx)
            with patch("modkeel.sources._linkage_rejection", return_value=Rejected("no")):
                RelaxedOfficialSource().deliver(self.CAND, MOD, ctx)
        assert downloads == ["https://cdn/mod.jar"]


class TestPossiblePorts:
    def test_only_range_rejections_are_listed(self):
        from modkeel.models import BranchCandidate

        def b(name, error, rng=None):
            br = BranchCandidate(name, "s", "")
            br.validation_error, br.version_range = error, rng
            return br

        branches = [b("port", "MC 1.21.10 not in declared range [1.21.1]", "[1.21.1]"),
                    b("fab", "Fabric-only mod (no neoforge version)"),
                    b("old", "MC version mismatch: 1.20.1 != 1.21.10")]
        found = possible_ports("alice/mod", branches)
        assert found == ["alice/mod port (declares [1.21.1])"]
        assert possible_ports_note(found).startswith("; possible ports not tried")
        assert possible_ports_note([]) == ""
        assert possible_ports_note(["a", "b", "c", "d", "e"]).endswith("a, b, c (+2 more)")


class TestServerBootRejection:
    def run(self, ctx, available=True, passed=True, error=None):
        from modkeel.sources import _server_boot_rejection

        def fake_test(self, results):
            results[0].docker_test_passed = passed
            results[0].docker_error = error

        with patch("modkeel.docker.DockerTester.check_docker_available", return_value=available), \
                patch("modkeel.docker.DockerTester.test_mods_in_docker", fake_test):
            return _server_boot_rejection(Path("m.jar"), [Path("dep.jar")], "M", ctx)

    def test_boots(self, ctx):
        assert self.run(ctx) is None

    def test_no_docker(self, ctx):
        assert "needs Docker" in self.run(ctx, available=False).reason

    def test_client_only_is_unverified(self, ctx):
        out = self.run(ctx, passed=None, error="[CLIENT-ONLY] Mod uses client-side classes")
        assert "inconclusive" in out.reason and "CLIENT-ONLY" in out.reason

    def test_crash(self, ctx):
        assert "did not boot" in self.run(ctx, passed=False, error="NoSuchMethodError").reason
