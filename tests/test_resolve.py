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
    _family,
    _linkage_rejection,
    default_strategies,
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
def ctx(tmp_path):
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
        assert [s.name for s in default_strategies()] == SOURCE_ORDER
        assert SOURCE_ORDER == ["official", "older_official", "fork"]


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
                patch("modkeel.sources._linkage_rejection", return_value=linkage):
            return OlderOfficialSource().deliver(cand, MOD, ctx)

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
        report = MagicMock(checked=True, is_clean=False, missing_classes=["a", "b"])
        with patch("modkeel.mappings.load_index", return_value=object()), \
                patch("modkeel.linkage.check_jar", return_value=report):
            out = _linkage_rejection(Path("x.jar"), "1.21.10")
        assert out.reason == "2 Minecraft classes it uses don't exist in 1.21.10"

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
