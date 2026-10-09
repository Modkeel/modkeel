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
from modkeel.evidence import FAILED, NOT_RUN, PASSED, Outcome
from modkeel.sources import (
    SOURCE_ORDER,
    ForkSource,
    OfficialSource,
    OlderOfficialSource,
    RelaxedOfficialSource,
    _family,
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

    def run_verified(self, ctx, tmp_path, outcomes, mods_path=None):
        """Two candidates, both delivered as real files; the server check is faked."""
        ctx.verify_runtime = True
        ctx.config.mods_path = mods_path
        ctx.config.output_dir.mkdir(parents=True, exist_ok=True)

        class FileSource(FakeSource):
            def deliver(self, candidate, mod, ctx):
                jar = ctx.config.output_dir / f"{candidate.label}.jar"
                jar.write_bytes(b"PK")
                if mods_path:
                    (mods_path / jar.name).write_bytes(b"PK")
                return Delivered(jar_path=jar, mod_name="m", mod_version="1",
                                 evidence=["metadata", "linkage"])

        outcomes = iter(outcomes)
        with patch("modkeel.evidence.check_server_boot", lambda s, c, events=None: next(outcomes)):
            return Resolver([FileSource("older_official", ["old1", "old2"])]).resolve(MOD, ctx)

    def test_runtime_crash_rejects_and_tries_the_next(self, ctx, tmp_path):
        """Monsters in the Closet 1.21.9 on 1.21.11 passed metadata and linkage, and its
        BedBlock mixin failed to apply on a real server: the next candidate gets a turn."""
        mods = tmp_path / "mods"
        mods.mkdir()
        res = self.run_verified(ctx, tmp_path, [
            Outcome("docker_server", FAILED, "a server did not boot with it (Mixin apply)"),
            Outcome("docker_server", PASSED, "booted")], mods_path=mods)
        assert res.delivered.jar_path.name == "old2.jar"
        assert res.delivered.evidence == ["metadata", "linkage", "docker_server"]
        assert [(s.ok, s.detail) for s in res.trail] == [
            (False, "old1: a server did not boot with it (Mixin apply)"), (True, "old2")]
        # the crashing JAR is gone from the output and the instance
        assert sorted(p.name for p in ctx.config.output_dir.iterdir()) == ["old2.jar"]
        assert sorted(p.name for p in mods.iterdir()) == ["old2.jar"]

    def test_runtime_check_that_cannot_run_keeps_the_jar(self, ctx, tmp_path):
        res = self.run_verified(ctx, tmp_path, [
            Outcome("docker_server", NOT_RUN, "needs Docker (a server must boot with it)")])
        assert res.delivered.jar_path.name == "old1.jar"
        assert res.delivered.unverified == "needs Docker (a server must boot with it)"
        assert "docker_server" not in res.delivered.evidence

    def test_no_second_boot_when_the_strategy_booted_one(self, ctx):
        ctx.verify_runtime = True
        booted = Delivered(jar_path=Path("x.jar"), mod_name="m", mod_version="1",
                           evidence=["docker_server"])

        class Booted(FakeSource):
            def deliver(self, candidate, mod, ctx):
                return booted

        with patch("modkeel.evidence.check_server_boot",
                   side_effect=AssertionError("booted twice")):
            res = Resolver([Booted("relaxed_official", ["r"])]).resolve(MOD, ctx)
        assert res.delivered is booted

    def test_default_order(self):
        assert SOURCE_ORDER == ["official", "curseforge", "official_source", "older_official",
                                "fork", "relaxed_official"]
        # get/search know no repo branches: official_source is skipped, order kept
        assert [s.name for s in default_strategies()] == [
            "official", "curseforge", "older_official", "fork", "relaxed_official"]

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

    def _deliver(self, ctx, valid=(True, "m", "1", "ok"), linkage=None, mixins=None):
        linkage = linkage or Outcome("linkage", PASSED, "ok")
        mixins = mixins or Outcome("mixins", PASSED, "ok")
        cand = Candidate("1 for MC 1.21.9",
                         {"version": version("1", ["1.21.9"]), "built_for": "1.21.9"})
        ctx.modrinth.download_modrinth_deps.return_value = []
        with patch("modkeel.sources._download",
                   lambda url, dest: dest.write_bytes(b"PK")), \
                patch("modkeel.evidence.validate_jar", return_value=valid), \
                patch("modkeel.evidence.check_mixins", return_value=mixins), \
                patch("modkeel.evidence.check_linkage", return_value=linkage) as check:
            out = OlderOfficialSource().deliver(cand, MOD, ctx)
        if valid[0]:
            # members are checked against the version the build targets
            subject = check.call_args.args[0]
            assert (subject.mc_version, subject.built_for) == (ctx.mc_version, "1.21.9")
        return out

    def test_metadata_must_allow_the_target(self, ctx):
        out = self._deliver(ctx, valid=(False, "m", "1",
                                        "JAR declares incompatible MC version: [1.21.9]"))
        assert isinstance(out, Rejected)
        assert out.reason == "its metadata only allows MC: [1.21.9]"
        assert not list(ctx.config.output_dir.iterdir())

    def test_broken_mixin_rejects(self, ctx):
        """Monsters in the Closet 1.0.3 on 1.21.11: metadata and linkage clean, its
        BedBlock @Inject handler no longer matches the target's parameters."""
        out = self._deliver(ctx, mixins=Outcome(
            "mixins", FAILED, "1 mixin injections would fail to apply on 1.21.10: BedBlockMixin"))
        assert isinstance(out, Rejected) and "would fail to apply" in out.reason
        assert not list(ctx.config.output_dir.iterdir())

    def test_mixin_check_that_cannot_run_does_not_reject(self, ctx):
        out = self._deliver(ctx, mixins=Outcome("mixins", NOT_RUN, "no symbol table"))
        assert isinstance(out, Delivered) and out.evidence == ["metadata", "linkage"]

    def test_linkage_must_pass(self, ctx):
        out = self._deliver(ctx, linkage=Outcome("linkage", FAILED,
                                                 "3 Minecraft classes it uses don't exist"))
        assert isinstance(out, Rejected) and "3 Minecraft classes" in out.reason
        assert not list(ctx.config.output_dir.iterdir())

    def test_delivered_with_evidence_and_caveat(self, ctx):
        out = self._deliver(ctx)
        assert isinstance(out, Delivered)
        assert out.evidence == ["metadata", "linkage", "mixins"]
        assert "Built for MC 1.21.9" in out.caveat
        assert "no method or field it calls was removed or renamed" in out.caveat
        assert (ctx.config.output_dir / "m.jar").exists()


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
    RANGE = (False, "m", "1", "JAR declares incompatible MC version: [1.21.9]")
    CAND = Candidate("1 for MC 1.21.9", {"version": version("1", ["1.21.9"], "mod.jar"),
                                          "built_for": "1.21.9"})

    def deliver(self, ctx, valid, linkage=None, relaxed=True, boot=None, mixins=None,
                relax_error=None):
        from modkeel.relax import Relaxed
        linkage = linkage or Outcome("linkage", PASSED, "ok")
        mixins = mixins or Outcome("mixins", PASSED, "ok")
        boot = boot or Outcome("docker_server", PASSED, "booted")
        # the source's own range check, then the evidence layer's on the rewritten JAR
        valid = iter(valid)
        ctx.modrinth.download_modrinth_deps.return_value = []
        downloads = []

        def fake_download(url, dest):
            downloads.append(url)
            dest.write_bytes(b"PK")

        def fake_relax(src, dest, built_for, target):
            if relax_error:
                raise relax_error
            if not relaxed:
                return None
            dest.write_bytes(b"PK relaxed")
            return Relaxed("META-INF/neoforge.mods.toml", "[1.21.9]", f"[1.21.9,{target}]")

        with patch("modkeel.sources._download", fake_download), \
                patch("modkeel.sources.validate_jar", side_effect=lambda *a, **k: next(valid)), \
                patch("modkeel.evidence.validate_jar", side_effect=lambda *a, **k: next(valid)), \
                patch("modkeel.evidence.check_linkage", return_value=linkage), \
                patch("modkeel.evidence.check_mixins", return_value=mixins), \
                patch("modkeel.relax.relax_jar", fake_relax), \
                patch("modkeel.evidence.check_server_boot", return_value=boot):
            out = RelaxedOfficialSource().deliver(self.CAND, MOD, ctx)
        return out, downloads

    def test_relaxed_and_marked(self, ctx):
        out, _ = self.deliver(ctx, valid=[self.RANGE, (True, "m", "1", "ok")])
        assert isinstance(out, Delivered)
        assert out.jar_path.name == "mod+modkeel-relaxed-mc1.21.10.jar"
        assert out.evidence == ["linkage", "mixins", "metadata_relaxed", "metadata",
                                "docker_server"]
        assert "declared [1.21.9]" in out.caveat and "server booted" in out.caveat

    def test_not_delivered_unless_a_server_boots(self, ctx):
        """TorchMaster 21.8.2 relaxed to 1.21.9 passed linkage and crashed on boot."""
        out, _ = self.deliver(ctx, valid=[self.RANGE, (True, "m", "1", "ok")],
                              boot=Outcome("docker_server", FAILED,
                                           "a server did not boot with it (NoSuchMethodError)"))
        assert isinstance(out, Rejected) and "did not boot" in out.reason
        assert not list(ctx.config.output_dir.glob("*.jar"))

    def test_metadata_already_allows_target(self, ctx):
        out, _ = self.deliver(ctx, valid=[(True, "m", "1", "ok")])
        assert isinstance(out, Rejected) and "older_official's case" in out.reason

    def test_linkage_must_pass(self, ctx):
        out, _ = self.deliver(ctx, valid=[self.RANGE],
                              linkage=Outcome("linkage", FAILED,
                                              "3 Minecraft classes it uses don't exist"))
        assert isinstance(out, Rejected) and "3 Minecraft classes" in out.reason
        assert not list(ctx.config.output_dir.glob("*.jar"))

    def test_no_docker_is_a_rejection(self, ctx):
        out, _ = self.deliver(ctx, valid=[self.RANGE, (True, "m", "1", "ok")],
                              boot=Outcome("docker_server", NOT_RUN,
                                           "needs Docker (a server must boot with it)"))
        assert isinstance(out, Rejected) and "needs Docker" in out.reason
        assert not list(ctx.config.output_dir.glob("*.jar"))

    def test_only_a_range_refusal_is_relaxed(self, ctx):
        """A refusal for any other reason (corrupt, no metadata) is not a range to widen."""
        out, _ = self.deliver(ctx, valid=[(False, None, None, "Invalid JAR file (corrupted)")])
        assert out.reason == ("refused for something other than its range "
                              "(Invalid JAR file (corrupted))")

    def test_unreadable_jar_is_a_rejection_not_a_crash(self, ctx):
        import zipfile
        out, _ = self.deliver(ctx, valid=[self.RANGE], relax_error=zipfile.BadZipFile("bad"))
        assert out.reason == "its metadata could not be rewritten (bad)"

    def test_nothing_to_rewrite(self, ctx):
        out, _ = self.deliver(ctx, valid=[self.RANGE], relaxed=False)
        assert out.reason == "no Minecraft range in its metadata to rewrite"

    def test_still_refused_after_rewrite_is_removed(self, ctx):
        out, _ = self.deliver(ctx, valid=[self.RANGE,
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

        refused = (False, "m", "1", "JAR declares incompatible MC version: x")
        with patch("modkeel.sources._download", fake_download), \
                patch("modkeel.sources.validate_jar", return_value=refused), \
                patch("modkeel.evidence.validate_jar", return_value=refused):
            OlderOfficialSource().deliver(self.CAND, MOD, ctx)
            with patch("modkeel.evidence.check_linkage",
                       return_value=Outcome("linkage", FAILED, "no")):
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


class TestWouldResolve:
    """The target layer's dry run: cheap strategies only, first candidate, nothing installed."""

    def strategy(self, name, cheap, candidates, verdicts, calls):
        from modkeel.resolve import Candidate, Found, Rejected, SourceStrategy

        class S(SourceStrategy):
            def find(self, mod, ctx):
                calls.append(("find", name))
                return Found([Candidate(c, {}) for c in candidates])

            def check(self, candidate, mod, ctx):
                calls.append(("check", candidate.label))
                return None if verdicts[candidate.label] else Rejected("no")

            def deliver(self, candidate, mod, ctx):
                raise AssertionError("would_resolve must not deliver")

        s = S()
        s.name, s.cheap = name, cheap
        return s

    def test_first_cheap_strategy_whose_check_passes(self):
        from modkeel.resolve import Resolver
        calls = []
        resolver = Resolver([
            self.strategy("official", True, [], {}, calls),
            self.strategy("older_official", True, ["a", "b"], {"a": True, "b": True}, calls),
            self.strategy("fork", False, ["f"], {"f": True}, calls)])
        assert resolver.would_resolve(MagicMock(), MagicMock()) == "older_official"
        assert calls == [("find", "official"), ("find", "older_official"), ("check", "a")]

    def test_slow_strategies_and_later_candidates_are_never_asked(self):
        from modkeel.resolve import Resolver
        calls = []
        resolver = Resolver([
            self.strategy("older_official", True, ["a", "b"], {"a": False, "b": True}, calls),
            self.strategy("fork", False, ["f"], {"f": True}, calls)])
        assert resolver.would_resolve(MagicMock(), MagicMock()) is None
        assert calls == [("find", "older_official"), ("check", "a")]

    def test_which_strategies_are_cheap(self):
        from modkeel.sources import default_strategies
        assert [s.name for s in default_strategies() if s.cheap] == [
            "official", "curseforge", "older_official"]
