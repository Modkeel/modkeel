"""Engine events (modkeel/core): terminal rendering, wire form, and who emits them.

The renderer must print exactly what the CLI printed before events existed (IDEA-028 step
1 changes no output); a front end that passes its own emitter must get every line as an
event and nothing on stdout.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from modkeel.core.events import (
    CarriedOver,
    CheckRan,
    Downloading,
    ForkChosen,
    Message,
    RangeRelaxed,
    Saved,
    SourceTried,
)
from modkeel.core.text import print_event, render_text
from modkeel.models import ModCompilerConfig
from modkeel.pipeline import Pipeline
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


class TestRenderText:
    """Each event renders to the line the code printed before (golden strings)."""

    @pytest.mark.parametrize("event, line", [
        (Message("\n  Tried:"), "\n  Tried:"),
        (Downloading("a.jar"), "    \U0001f4e5 Downloading a.jar..."),
        (Downloading("a.jar", purpose="check", built_for="1.21.8"),
         "    \U0001f4e5 Checking a.jar (built for MC 1.21.8)..."),
        (Saved(Path("/out/a.jar")), "    \U0001f4be Saved: /out/a.jar"),
        (Saved(Path("/mods/a.jar"), installed=True), "    \U0001f4be Installed: /mods/a.jar"),
        (CheckRan("linkage", "passed", "120 classes"), "    ✓ Linkage: 120 classes"),
        (CheckRan("mixins", "passed", "8 targets"), "    ✓ Mixins: 8 targets"),
        (CheckRan("linkage", "not_run", "no mappings"), "    \U0001f50e Linkage: no mappings"),
        (CheckRan("linkage", "failed", "3 missing -- JAR targets a different version"),
         "    ⚠️  Linkage: 3 missing -- JAR targets a different version"),
        (RangeRelaxed("fabric.mod.json", "~1.21.8", "~1.21.8 || 1.21.9"),
         "    ✏️  Relaxed fabric.mod.json: MC ~1.21.8 -> ~1.21.8 || 1.21.9"),
        (ForkChosen("me/mod (1.21.10) score 9"), "\n  Best fork: me/mod (1.21.10) score 9"),
        (CarriedOver("Ctrl Q", "1.21.10", "1.21.11", True, "metadata, linkage"),
         "  ↻ Ctrl Q: the MC 1.21.11 JAR also passes on 1.21.10 (metadata, linkage)"),
        (CarriedOver("Monsters", "1.21.11", "1.21.9", False, "linkage: 2 missing"),
         "  ↻ Monsters: not reused for MC 1.21.11 (linkage: 2 missing)"),
    ])
    def test_same_line_as_before(self, event, line):
        assert render_text(event) == line

    def test_trail_steps_are_not_printed_live(self, capsys):
        # the CLI prints the whole trail at the end; live steps are for other front ends
        print_event(SourceTried("Mod", "official", True, "Mod 1.0"))
        assert capsys.readouterr().out == ""


class TestWireForm:
    def test_to_dict_is_json_with_its_kind(self):
        data = Saved(Path("/out/a.jar"), installed=True).to_dict()
        assert data == {"kind": "saved", "path": "/out/a.jar", "installed": True}
        assert json.loads(json.dumps(data)) == data

    def test_every_event_kind_is_distinct(self):
        kinds = [cls.kind for cls in (Message, Downloading, Saved, CheckRan, RangeRelaxed,
                                      ForkChosen, SourceTried, CarriedOver)]
        assert len(set(kinds)) == len(kinds)


class _Fixed(SourceStrategy):
    """A strategy with canned answers, to watch what the resolver reports."""

    def __init__(self, name, found, outcome=None):
        self.name, self._found, self._outcome = name, found, outcome

    def find(self, mod, ctx):
        return self._found

    def deliver(self, candidate, mod, ctx):
        return self._outcome


class TestResolverEvents:
    def test_every_trail_step_is_emitted_as_it_happens(self, tmp_path, capsys):
        events = []
        ctx = ResolveContext(config=MagicMock(), modrinth=MagicMock(), events=events.append)
        jar = tmp_path / "a.jar"
        resolver = Resolver([
            _Fixed("official", Found(note="no build for 1.21.10")),
            _Fixed("older_official", Found([Candidate("Mod 1.0")]), Rejected("linkage failed")),
            _Fixed("fork", Found([Candidate("me/mod")]), Delivered(jar, "Mod", "1.0")),
        ])
        resolution = resolver.resolve(ModRef("mod"), ctx)

        tried = [e for e in events if isinstance(e, SourceTried)]
        assert [(e.strategy, e.ok, e.detail) for e in tried] == [
            (s.strategy, s.ok, s.detail) for s in resolution.trail]
        assert tried[-1].ok and tried[0].mod == "mod"
        assert capsys.readouterr().out == ""


@pytest.fixture
def quiet_pipeline(tmp_path, monkeypatch):
    """A Pipeline with doubles (as in test_pipeline) whose events go to a list."""
    monkeypatch.setattr("modkeel.sources.MODKEEL_HOME", tmp_path / "home")
    config = ModCompilerConfig(mc_version="1.21.10", loader="neoforge", loader_version="64",
                               output_dir=str(tmp_path / "out"))
    events = []
    p = Pipeline(config, events=events.append)
    p.temp_dir = str(tmp_path / "work")
    Path(p.temp_dir).mkdir()
    p.github = MagicMock()
    p.github.get_repo_info.return_value = {"stargazers_count": 1, "forks_count": 0}
    p.github.search_compatible_repos.return_value = []
    p.validator = MagicMock()
    p.modrinth = MagicMock()
    p.prebuild = MagicMock()
    p.docker = MagicMock()
    return p, events


class TestPipelineEvents:
    def test_a_front_end_gets_the_run_as_events_and_no_stdout(self, quiet_pipeline, capsys):
        pipeline, events = quiet_pipeline
        pipeline.modrinth.check_modrinth.return_value = {
            "download_url": "https://cdn/x.jar", "filename": "x.jar",
            "title": "Mod", "version_number": "2.0",
        }
        with patch("modkeel.pipeline.requests.get") as get:
            get.return_value = MagicMock(content=b"bytes")
            result = pipeline.clone_and_compile("https://github.com/owner/mod")

        assert result.success
        assert capsys.readouterr().out == ""
        assert any(isinstance(e, Message) and "Processing" in e.text for e in events)
        delivered = [e for e in events if isinstance(e, SourceTried) and e.ok]
        assert len(delivered) == 1 and delivered[0].mod == "mod"
