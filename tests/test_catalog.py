"""The "Add a mod" search (modkeel/core/catalog.py): identity and status per hit."""

from unittest.mock import patch

import pytest
import requests

from modkeel.core import catalog
from modkeel.core.catalog import (OFFICIAL, OTHER_LOADER, OTHER_VERSIONS, CatalogError,
                                  builds_check, search_mods)
from modkeel.scanner import compute_sha1


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def hit(pid, loaders=("fabric",), versions=("1.21.11",), **extra):
    return {"project_id": pid, "slug": pid, "title": pid.title(), "author": "cortex",
            "downloads": 7242602, "icon_url": f"https://cdn/{pid}.png", "description": "d",
            "categories": list(loaders), "versions": list(versions), **extra}


def serve(hits, builds=None, status=200):
    """Modrinth: the search answers `hits`; /project/<id>/version answers builds[id]."""
    calls = []

    def call(method, url, **kw):
        calls.append((url, kw.get("params")))
        if url.endswith("/search"):
            return Resp(status, {"hits": hits})
        pid = url.split("/project/")[1].split("/")[0]
        found = (builds or {}).get(pid)
        return Resp(503) if found is None else Resp(200, found)

    return patch.object(catalog, "modrinth_call", call), calls


class TestSearch:
    def test_each_hit_has_identity_page_and_status(self):
        p, calls = serve([hit("voxy"), hit("forge-only", loaders=("forge",)),
                          hit("old", versions=("1.21.1",)), hit("mixed")],
                         builds={"voxy": [{"id": "v"}], "mixed": []})
        with p:
            hits = search_mods("voxy", "1.21.11", "Fabric")
        assert [(h.project_id, h.status) for h in hits] == [
            ("voxy", OFFICIAL), ("forge-only", OTHER_LOADER), ("old", OTHER_VERSIONS),
            ("mixed", OTHER_VERSIONS)]
        first = hits[0].to_dict()
        assert first["page_url"] == "https://modrinth.com/mod/voxy"
        assert first["author"] == "cortex" and first["downloads"] == 7242602
        assert first["icon_url"] == "https://cdn/voxy.png"
        # only hits whose lists say yes cost a versions call, with loader and version
        assert [u for u, _ in calls if "/version" in u] == [
            "https://api.modrinth.com/v2/project/voxy/version",
            "https://api.modrinth.com/v2/project/mixed/version"]
        assert calls[1][1] == {"loaders": '["fabric"]', "game_versions": '["1.21.11"]'}

    def test_versions_call_failing_keeps_what_the_hit_said(self):
        p, _ = serve([hit("voxy")], builds={})
        with p:
            assert search_mods("voxy", "1.21.11", "fabric")[0].status == OFFICIAL

    def test_hits_without_identity_are_dropped(self):
        p, _ = serve([{"slug": "x"}, hit("voxy")], builds={"voxy": [{}]})
        with p:
            assert [h.project_id for h in search_mods("v", "1.21.11", "fabric")] == ["voxy"]

    @pytest.mark.parametrize("failure, message", [
        (requests.Timeout(), "Modrinth timed out"),
        (requests.ConnectionError(), "Modrinth unreachable: ConnectionError"),
    ])
    def test_network_errors_raise_with_the_reason(self, failure, message):
        def call(*a, **kw):
            raise failure
        with patch.object(catalog, "modrinth_call", call), \
                pytest.raises(CatalogError, match=message):
            search_mods("x", "1.21.11", "fabric")

    def test_http_error_and_bad_body_raise(self):
        p, _ = serve([], status=500)
        with p, pytest.raises(CatalogError, match="HTTP 500"):
            search_mods("x", "1.21.11", "fabric")
        with patch.object(catalog, "modrinth_call",
                          lambda *a, **k: Resp(200, ValueError("bad"))), \
                pytest.raises(CatalogError, match="unreadable"):
            search_mods("x", "1.21.11", "fabric")


class TestBuildsCheck:
    """An instance's mods with no build for its own version and loader, by hash."""

    def folder(self, tmp_path, *names):
        for n in names:
            (tmp_path / n).write_bytes(n.encode())
        (tmp_path / "notes.txt").write_text("not a mod")
        return {n: compute_sha1(tmp_path / n) for n in names}

    def serve(self, known, have, projects, fail=None):
        calls = []

        def call(method, url, **kw):
            calls.append((url, kw.get("json"), kw.get("params")))
            if fail and fail in url:
                raise requests.ConnectionError()
            if url.endswith("/version_files"):
                return Resp(200, known)
            if url.endswith("/version_files/update"):
                return Resp(200, have)
            return Resp(200, projects)

        return patch.object(catalog, "modrinth_call", call), calls

    def test_known_files_without_a_build_are_named_unknown_ones_only_counted(self, tmp_path):
        h = self.folder(tmp_path, "sodium.jar", "ironchests.jar", "local.jar")
        p, calls = self.serve(
            known={h["sodium.jar"]: {"project_id": "AANobbMI"},
                   h["ironchests.jar"]: {"project_id": "P3iIrPH3"}},
            have={h["sodium.jar"]: {"project_id": "AANobbMI"}},
            projects=[{"id": "P3iIrPH3", "slug": "iron-chests", "title": "Iron Chests"}])
        with p:
            check = builds_check(str(tmp_path), "1.21.1", "NeoForge")
        assert (check.mods, check.known, check.reason) == (3, 2, "")
        assert [(m.file, m.slug, m.title) for m in check.missing] == [
            ("ironchests.jar", "iron-chests", "Iron Chests")]
        update = calls[1][1]
        assert update["loaders"] == ["neoforge"] and update["game_versions"] == ["1.21.1"]
        assert sorted(update["hashes"]) == sorted([h["sodium.jar"], h["ironchests.jar"]])

    def test_every_build_there_asks_no_project_names(self, tmp_path):
        h = self.folder(tmp_path, "a.jar")
        known = {h["a.jar"]: {"project_id": "A"}}
        p, calls = self.serve(known=known, have=known, projects=[])
        with p:
            check = builds_check(str(tmp_path), "1.21.11", "fabric")
        assert check.missing == [] and len(calls) == 2

    def test_modrinth_down_is_a_reason_not_an_error(self, tmp_path):
        self.folder(tmp_path, "a.jar")
        p, _ = self.serve({}, {}, [], fail="version_files")
        with p:
            check = builds_check(str(tmp_path), "1.21.11", "fabric")
        assert check.reason == "Modrinth unreachable: ConnectionError" and check.missing == []

    def test_names_failing_still_names_the_files(self, tmp_path):
        h = self.folder(tmp_path, "a.jar")
        p, _ = self.serve({h["a.jar"]: {"project_id": "A"}}, {}, [], fail="/projects")
        with p:
            check = builds_check(str(tmp_path), "1.21.11", "fabric")
        assert [(m.file, m.title) for m in check.missing] == [("a.jar", "a.jar")]

    def test_an_empty_folder_asks_nothing(self, tmp_path):
        p, calls = self.serve({}, {}, [])
        with p:
            assert builds_check(str(tmp_path), "1.21.11", "fabric").mods == 0
        assert calls == []
