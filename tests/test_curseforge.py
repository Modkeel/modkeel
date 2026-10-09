"""CurseForge through Modkeel's service (modkeel/curseforge.py) and the curseforge source
strategy (modkeel/sources.py).

The fingerprint against Austin Appleby's reference MurmurHash2 (values from the C code); the
client against a scripted service (answers, the service being off, the per-run cache); the
strategy with a scripted client: found, skipped while off, another mod under the same slug,
the download checked against CurseForge's SHA-1, files only CurseForge may serve; and `move`
naming a JAR by its fingerprint.
"""

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from modkeel.core.events import Message
from modkeel.curseforge import (
    CfFile,
    CfMod,
    CfProject,
    CurseForgeClient,
    default_client,
    fingerprint,
    murmur2,
)
from modkeel.models import ModCompilerConfig
from modkeel.resolve import Delivered, ModRef, Rejected, ResolveContext, Resolver
from modkeel.sources import CurseForgeSource

URL = "https://cf.test/v1/cf"


class FakeResponse:
    def __init__(self, status: int, body=None):
        self.status_code = status
        self.body = body

    def json(self):
        if self.body is None:
            raise ValueError("no body")
        return self.body


class FakeService:
    """Answers by path; records (method, path, kwargs) of every request."""

    def __init__(self, answers=None, status=200, error=None):
        self.answers, self.status, self.error = answers or {}, status, error
        self.calls = []

    def request(self, method, url, timeout=None, headers=None, **kwargs):
        path = url[len(URL):]
        self.calls.append((method, path, kwargs))
        if self.error:
            raise self.error
        if self.status != 200:
            return FakeResponse(self.status, {"error": f"said {self.status}"})
        answer = self.answers[path]
        return FakeResponse(200, answer(kwargs) if callable(answer) else answer)


MOD = {"id": 394468, "slug": "sodium", "name": "Sodium",
       "url": "https://www.curseforge.com/minecraft/mc-mods/sodium",
       "source": "https://github.com/CaffeineMC/sodium", "distribution": True}


def cf_file(**over):
    data = {"id": 6001, "mod": 394468, "name": "sodium-0.6.13.jar", "display": "Sodium 0.6.13",
            "mc": ["1.21.1"], "loaders": ["fabric"], "type": "release",
            "date": "2025-04-01T00:00:00Z", "url": "https://edge.forgecdn.net/6001/sodium.jar",
            "sha1": None, "fingerprint": 1, "requires": []}
    data.update(over)
    return data


class TestFingerprint:
    # MurmurHash2 with seed 1 (the C reference), over the bytes without whitespace
    @pytest.mark.parametrize("text, expected", [
        (b"", 1540447798), (b"a", 626045324), (b"ab", 1692487918), (b"abc", 1621425345),
        (b"abcd", 3376380438),
        (b"The quick brown fox jumps over the lazy dog", 3751777527),
    ])
    def test_matches_the_reference(self, tmp_path, text, expected):
        path = tmp_path / "f.bin"
        path.write_bytes(text)
        assert fingerprint(path) == expected

    def test_whitespace_bytes_do_not_count(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.write_bytes(b"hello\r\n\tworld ")
        b.write_bytes(b"helloworld")
        assert fingerprint(a) == fingerprint(b) == murmur2(b"helloworld")

    def test_binary_content(self):
        data = bytes(range(256)) * 3 + b"\x01\x02"
        assert murmur2(data) == murmur2(data, seed=1) != murmur2(data, seed=2)


class TestClient:
    def test_off_without_an_address(self):
        client = CurseForgeClient("")
        assert not client.available
        assert client.files("sodium", "1.21.1", "fabric") is None
        assert client.match([Path("never-read.jar")]) == {}

    def test_the_run_shares_one_client_off_in_tests(self):
        # conftest empties MODKEEL_CURSEFORGE_URL: no test reaches api.modkeel.com
        assert default_client() is default_client()
        assert not default_client().available

    @pytest.mark.parametrize("status", [404, 502, 503])
    def test_an_off_answer_turns_lookups_off_for_the_run(self, status):
        service = FakeService(status=status)
        client = CurseForgeClient(URL, service)
        assert client.files("sodium", "1.21.1", "fabric") is None
        assert client.files("jei", "1.21.1", "fabric") is None
        assert not client.available and client.last_error == f"said {status}"
        assert len(service.calls) == 1

    def test_unreachable_turns_lookups_off(self):
        client = CurseForgeClient(URL, FakeService(error=requests.ConnectionError("down")))
        assert client.files("sodium", "1.21.1", "fabric") is None
        assert not client.available and "ConnectionError" in client.last_error

    def test_a_refused_question_fails_only_itself(self):
        client = CurseForgeClient(URL, FakeService(status=400))
        assert client.files("sodium", "1.21.1", "fabric") is None
        assert client.available

    def test_files_are_asked_once_per_run(self):
        service = FakeService({"/files": {"mod": MOD, "files": [cf_file()]}})
        client = CurseForgeClient(URL, service)
        first = client.files("Sodium", "1.21.1", "Fabric")
        assert client.files("sodium", "1.21.1", "fabric") is first
        assert first.mod == CfMod.of(MOD) and first.files[0].url.endswith("sodium.jar")
        assert service.calls == [("GET", "/files", {"params": {
            "mod": "sodium", "mc": "1.21.1", "loader": "fabric"}})]

    def test_an_unknown_project(self):
        client = CurseForgeClient(URL, FakeService({"/files": {"mod": None, "files": []}}))
        assert client.files("nope", "1.21.1", "fabric") == CfProject(None, [])

    def test_match_names_each_jar_and_sends_each_fingerprint_once(self, tmp_path):
        a, same, other = tmp_path / "a.jar", tmp_path / "copy.jar", tmp_path / "b.jar"
        a.write_bytes(b"PK one")
        same.write_bytes(b"PK one")
        other.write_bytes(b"PK two")

        def answer(kwargs):
            assert sorted(kwargs["json"]["fingerprints"]) == sorted(
                {fingerprint(a), fingerprint(other)})
            return {"matches": [{"fingerprint": fingerprint(a), "mod": MOD,
                                 "file": cf_file(fingerprint=fingerprint(a))}]}

        client = CurseForgeClient(URL, FakeService({"/fingerprints": answer}))
        found = client.match([a, same, other])
        assert set(found) == {a, same}
        assert found[a].mod.slug == "sodium" and found[a].file.name == "sodium-0.6.13.jar"

    def test_no_hashing_while_off(self, tmp_path):
        with patch("modkeel.curseforge.fingerprint", side_effect=AssertionError("hashed")):
            assert CurseForgeClient("").match([tmp_path / "x.jar"]) == {}

    @pytest.mark.parametrize("source, repo", [
        ("https://github.com/CaffeineMC/sodium", "CaffeineMC/sodium"),
        ("https://github.com/CaffeineMC/sodium.git/", "CaffeineMC/sodium"),
        ("https://gitlab.com/a/b", None), (None, None),
    ])
    def test_github_repo_of_the_source_link(self, source, repo):
        assert CfMod(1, "s", "S", source=source).github_repo == repo


class FakeClient:
    """A CurseForgeClient double: one project per ref."""

    def __init__(self, projects=None, available=True):
        self.projects = projects or {}
        self.available, self.last_error = available, None
        self.asked = []

    def files(self, ref, mc, loader):
        self.asked.append(ref)
        return self.projects.get(ref, CfProject(None, []))


def ctx_for(tmp_path, client, events=None):
    config = ModCompilerConfig(mc_version="1.21.1", loader="fabric", loader_version="0",
                               output_dir=str(tmp_path / "out"))
    config.output_dir.mkdir(parents=True, exist_ok=True)
    return ResolveContext(config=config, modrinth=None, curseforge=client,
                          events=events.append if events is not None else (lambda e: None))


def project(*files, **mod):
    return CfProject(CfMod.of({**MOD, **mod}), [CfFile.of(f) for f in files])


def sodium_on_modrinth() -> ModRef:
    """A fresh ModRef each time: the strategy records the CurseForge project it finds on it."""
    return ModRef("sodium", project={"project_id": "AANobbMI", "slug": "sodium",
                                     "title": "Sodium"})


class TestStrategy:
    def test_skipped_while_the_service_is_off(self, tmp_path):
        ctx = ctx_for(tmp_path, FakeClient(available=False))
        assert CurseForgeSource().find(sodium_on_modrinth(), ctx).skipped
        resolution = Resolver([CurseForgeSource()]).resolve(sodium_on_modrinth(), ctx)
        assert resolution.trail == [] and resolution.delivered is None

    def test_finds_the_build_by_the_modrinth_slug_and_learns_the_project(self, tmp_path):
        client = FakeClient({"sodium": project(
            cf_file(id=2, type="beta", display="Sodium 0.7 beta", date="2025-05-01"),
            cf_file(id=1))})
        mod = ModRef("sodium", project={"project_id": "x", "slug": "sodium", "title": "Sodium"})
        found = CurseForgeSource().find(mod, ctx_for(tmp_path, client))
        # the newest release, not the newer beta
        assert [c.label for c in found.candidates] == ["Sodium 0.6.13 (release)"]
        assert mod.curseforge.id == 394468 and mod.source_repo == "CaffeineMC/sodium"

    def test_a_known_project_is_asked_by_id(self, tmp_path):
        client = FakeClient({"394468": project(cf_file())})
        mod = ModRef("Sodium", curseforge=CfMod(394468, "sodium", "Sodium"))
        assert CurseForgeSource().find(mod, ctx_for(tmp_path, client)).candidates
        assert client.asked == ["394468"] and mod.title == "Sodium"

    def test_another_mod_under_the_same_slug_is_not_taken(self, tmp_path):
        client = FakeClient({"sodium": project(cf_file(), name="Sodium Dynamic Lights")})
        mod = ModRef("sodium", project={"project_id": "x", "slug": "sodium",
                                        "title": "Iris Shaders"})
        found = CurseForgeSource().find(mod, ctx_for(tmp_path, client))
        assert not found.candidates and "another mod" in found.note
        assert mod.curseforge is None

    def test_no_build_for_the_target(self, tmp_path):
        found = CurseForgeSource().find(sodium_on_modrinth(),
                                        ctx_for(tmp_path, FakeClient({"sodium": project()})))
        assert found.note == "Sodium has no Fabric build for MC 1.21.1 on CurseForge"

    def test_not_on_curseforge(self, tmp_path):
        found = CurseForgeSource().find(sodium_on_modrinth(), ctx_for(tmp_path, FakeClient()))
        assert found.note == "Not found on CurseForge"

    def test_delivers_the_file_checked_against_its_sha1(self, tmp_path):
        body = b"PK the jar"
        events = []
        client = FakeClient({"sodium": project(cf_file(sha1=hashlib.sha1(body).hexdigest(),
                                                       requires=[306612])),
                             "306612": project(id=306612, name="Fabric API")})
        ctx = ctx_for(tmp_path, client, events)
        with patch("modkeel.sources._download", lambda url, dest: dest.write_bytes(body)):
            resolution = Resolver([CurseForgeSource()]).resolve(sodium_on_modrinth(), ctx)
        delivered = resolution.delivered
        assert isinstance(delivered, Delivered) and delivered.evidence == ["published"]
        assert delivered.jar_path.read_bytes() == body
        assert [s.strategy for s in resolution.trail] == ["curseforge"]
        # required projects are named, not fetched
        assert "Sodium also needs: Fabric API (CurseForge)" in [
            e.text.strip() for e in events if isinstance(e, Message)]

    def test_a_corrupt_download_is_refused_and_removed(self, tmp_path):
        client = FakeClient({"sodium": project(cf_file(sha1="0" * 40))})
        ctx, mod = ctx_for(tmp_path, client), sodium_on_modrinth()
        candidate = CurseForgeSource().find(mod, ctx).candidates[0]
        with patch("modkeel.sources._download", lambda url, dest: dest.write_bytes(b"other")):
            outcome = CurseForgeSource().deliver(candidate, mod, ctx)
        assert isinstance(outcome, Rejected) and "SHA-1" in outcome.reason
        assert not (ctx.config.output_dir / "sodium-0.6.13.jar").exists()

    def test_a_file_only_curseforge_may_serve_sends_the_player_there(self, tmp_path):
        client = FakeClient({"sodium": project(cf_file(url=None), distribution=False)})
        ctx, mod = ctx_for(tmp_path, client), sodium_on_modrinth()
        candidate = CurseForgeSource().find(mod, ctx).candidates[0]
        with patch("modkeel.sources._download", side_effect=AssertionError("downloaded")):
            outcome = CurseForgeSource().deliver(candidate, mod, ctx)
        assert outcome.reason == ("its author allows downloads only from CurseForge: "
                                  "https://www.curseforge.com/minecraft/mc-mods/sodium")
        # the target layer judges it the same way
        assert CurseForgeSource().check(candidate, mod, ctx) == outcome


class TestMove:
    def test_a_jar_named_only_by_its_fingerprint(self, tmp_path, monkeypatch):
        from modkeel.core.move import MoveRequest, move_pack, scan_pack
        from modkeel.modrinth import ModrinthClient
        from modkeel.resolve import Resolution, Step

        monkeypatch.chdir(tmp_path)
        mods = tmp_path / "mods"
        mods.mkdir()
        (mods / "secret.jar").write_bytes(b"PK not a zip")

        def answer(kwargs):
            return {"matches": [{"fingerprint": kwargs["json"]["fingerprints"][0],
                                 "mod": {**MOD, "id": 7, "slug": "secret", "name": "Secret Mod",
                                         "source": None}, "file": cf_file()}]}

        client = CurseForgeClient(URL, FakeService({"/fingerprints": answer}))
        seen = []

        def resolve(self, mod, ctx):
            seen.append(mod)
            return Resolution(trail=[Step("curseforge", False, "Secret Mod has no build")])

        with patch.object(ModrinthClient, "versions_by_hash", lambda self, h: {}), \
                patch.object(ModrinthClient, "fetch_projects", lambda self, ids: {}), \
                patch.object(ModrinthClient, "fetch_project", lambda self, pid: None), \
                patch.object(ModrinthClient, "find_project", lambda self, q: (None, [])), \
                patch("modkeel.curseforge.default_client", return_value=client), \
                patch("modkeel.resolve.Resolver.resolve", resolve):
            entries = scan_pack(mods, ModrinthClient(_config(), lambda e: None), lambda e: None)
            assert [(e.mod.name, e.mod.identified_by, e.curseforge.id) for e in entries] == [
                ("Secret Mod", "fingerprint", 7)]
            result = move_pack(MoveRequest(str(mods), "1.21.10"), events=lambda e: None)
        assert seen[0].curseforge.slug == "secret" and seen[0].project is None
        secret = result.mods[0]
        assert secret.status == "missing" and secret.detail.startswith("Secret Mod has no build")


def _config():
    return ModCompilerConfig(mc_version="1.21.10", loader="fabric", loader_version="0")


def test_pack_line_counts_curseforge_fingerprints():
    from modkeel.core.events import PackScanned
    from modkeel.core.text import render_text

    line = render_text(PackScanned("mods", (("a.jar", "A", "a", "hash"),
                                            ("b.jar", "B", None, "fingerprint"))))
    assert "1 identified by their hash, 1 by CurseForge's fingerprint, 0 by name" in line


def test_wire_form_keeps_the_fingerprint_label():
    from modkeel.core.events import PackScanned

    event = PackScanned("mods", (("b.jar", "B", None, "fingerprint"),))
    assert json.loads(json.dumps(event.to_dict()))["mods"][0][3] == "fingerprint"
