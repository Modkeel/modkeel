"""Engine entry points: the orchestration of a run, without terminal I/O (IDEA-028 step 2).

A front end says what the user wants (a Request); the engine decides how. Progress goes out
as events (`events=`, modkeel.core.events), questions only the user can answer go through
`decide=` (modkeel.core.decisions), and `cancelled=` is checked between steps. The CLI
commands parse arguments, render events and answer questions; they hold no flow of their own.

    get_mod(GetRequest)     one mod: identify it, run the sources on the target, and when
                            nothing runs there propose the nearest version where it does,
                            ask (ChangeTarget) and run there
    retarget_pack(...)      compile's version change after a pipeline run: propose a version
                            for the whole list, ask, reuse what passes there, re-run the rest

A version change never writes into the instance and drops -lv (it named a loader build for
the version asked for); the output goes to <output>/mc-<version>/ (target.fallback_output).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from modkeel.core.decisions import (
    Cancel,
    ChangeTarget,
    Decide,
    NeedToken,
    check_cancel,
    safe_default,
)
from modkeel.core.events import (
    Delivery,
    Emitter,
    ModIdentified,
    ModResolved,
    TargetSearch,
)
from modkeel.core.text import print_event
from modkeel.models import CompilationResult, ModCompilerConfig


@dataclass
class GetRequest:
    """What `get` asks for: one mod (name, slug or project), a target, where to put it."""

    query: str
    mc_version: str
    loader: str
    loader_version: Optional[str] = None  # needed only to compile a fork
    output_dir: str = "out"
    instance: Optional[str] = None
    verify_runtime: bool = False          # boot a server with the result (--docker-test)
    github_token: Optional[str] = None    # without one, NeedToken is asked when forks are needed


@dataclass
class GetResult:
    """How a get ended. retargeted: it ran on `target`, a version other than the one asked."""

    mod: object                           # resolve.ModRef
    target: str
    output_dir: Path
    delivered: object = None              # resolve.Delivered, or None
    retargeted: bool = False
    proposal: object = None               # target.TargetOption proposed, taken or not


def _delivery(delivered) -> Optional[Delivery]:
    if delivered is None:
        return None
    return Delivery(Path(delivered.jar_path), delivered.mod_name, delivered.mod_version,
                    delivered.verb, tuple(delivered.evidence), delivered.caveat,
                    delivered.unverified)


def get_mod(request: GetRequest, events: Emitter = print_event,
            decide: Decide = safe_default, cancelled: Optional[Cancel] = None) -> GetResult:
    """Get one mod for the request's target, or for the nearest version the user accepts."""
    from modkeel.modrinth import ModrinthClient
    from modkeel.resolve import ResolveContext, Resolver
    from modkeel.sources import identify_mod

    loader = request.loader.lower()
    token: List[Optional[str]] = [request.github_token]
    asked: List[bool] = [False]

    def make_config(tok: Optional[str], mc: str, out: str, lv: Optional[str],
                    inst: Optional[str]) -> ModCompilerConfig:
        # "0" marks "no loader version given": lookups don't need one, compiling does.
        return ModCompilerConfig(mc_version=mc, loader=loader, loader_version=lv or "0",
                                 github_token=tok, output_dir=out, instance_path=inst)

    def token_on_demand() -> Optional[str]:
        """Called by the fork strategy: the token is asked for once, only when needed."""
        if not token[0] and not asked[0]:
            asked[0] = True
            token[0] = decide(NeedToken("forks"))
        return token[0]

    config = make_config(token[0], request.mc_version, request.output_dir,
                         request.loader_version, request.instance)
    modrinth = ModrinthClient(config, events)
    mod = identify_mod(request.query, modrinth)
    events(ModIdentified(
        request.query, mod.title, mod.project is not None,
        slug=mod.project.get("slug") if mod.project else None, source_repo=mod.source_repo,
        related=tuple((h["title"], h.get("slug", "?")) for h in mod.related if h.get("title"))))

    def resolve_on(cfg: ModCompilerConfig, retarget: bool):
        check_cancel(cancelled)
        # The client fetches dependencies for its own config's version and output folder:
        # another target needs its own, or they would come for the version asked for and
        # land beside its files.
        client = ModrinthClient(cfg, events) if retarget else modrinth
        ctx = ResolveContext(
            config=cfg, modrinth=client, github_token=token_on_demand,
            make_config=lambda t: make_config(
                t, cfg.mc_version, str(cfg.output_dir), cfg.loader_version,
                str(cfg.instance_path) if cfg.instance_path else None),
            verify_runtime=request.verify_runtime, events=events)
        resolution = Resolver().resolve(mod, ctx)
        events(ModResolved(mod.title, cfg.mc_version,
                           tuple((s.strategy, s.ok, s.detail) for s in resolution.trail),
                           _delivery(resolution.delivered), retarget, request.verify_runtime))
        return resolution.delivered

    result = GetResult(mod, request.mc_version, Path(request.output_dir))
    result.delivered = resolve_on(config, retarget=False)
    if result.delivered or not mod.project:
        return result

    option = _propose(
        [(mod.title, mod.project.get("project_id"))], loader, request.mc_version, modrinth,
        resolved=0, scope="mod", subject=mod.title, events=events)
    result.proposal = option
    if option is None or not decide(ChangeTarget(option, request.mc_version, "mod")):
        return result

    from modkeel.target import fallback_output

    out = fallback_output(request.output_dir, option.mc_version)
    result.target, result.output_dir, result.retargeted = option.mc_version, out, True
    # A different Minecraft version: never into the instance, and no -lv (Docker picks the
    # loader for this one).
    result.delivered = resolve_on(make_config(token[0], option.mc_version, str(out), None,
                                              None), retarget=True)
    return result


def _propose(mods: Sequence[Tuple[str, Optional[str]]], loader: str, current: str, modrinth,
             resolved: int, scope: str, subject: str, events: Emitter):
    """The target layer's best proposal (official builds, older builds probed near the
    target), or None. Looked up at call time so tests can replace the probe and releases."""
    from modkeel import mappings, target

    events(TargetSearch(scope, subject))
    options = target.propose_targets(
        mods, loader, current, modrinth, resolved, limit=1,
        probe=target.older_build_probe(loader, modrinth), releases=mappings.release_versions())
    return options[0] if options else None


@dataclass
class PackRetarget:
    """compile's run on a proposed version: where it went and what it built."""

    option: object                        # target.TargetOption
    config: ModCompilerConfig             # the run's config (output_dir = mc-<version>/)
    results: List[CompilationResult] = field(default_factory=list)
    carried: Dict[str, CompilationResult] = field(default_factory=dict)
    report: str = ""

    @property
    def built(self) -> int:
        return sum(1 for r in self.results if r.success)


def retarget_pack(repo_urls: Sequence[str], first_results: Sequence[CompilationResult],
                  config: ModCompilerConfig, events: Emitter = print_event,
                  decide: Decide = safe_default, cancelled: Optional[Cancel] = None,
                  pipeline_factory: Optional[Callable] = None) -> Optional[PackRetarget]:
    """After a compile run with failures: the nearest version where more of the list runs.

    The pack's mods are the repos' Modrinth projects (repos without one count as missing
    everywhere). None when there is no proposal or the user keeps the version. What the
    first run resolved is reused when it also passes there (target.carry_over), except mods
    with an official build on the new version: that build wins.
    """
    from modkeel import target
    from modkeel.github import parse_repo_url
    from modkeel.modrinth import ModrinthClient
    from modkeel.pipeline import Pipeline
    from modkeel.sources import identify_repo

    make_pipeline = pipeline_factory or Pipeline
    modrinth = ModrinthClient(config, events)
    mods: List[Tuple[str, Optional[str]]] = []   # parallel to repo_urls
    for url in repo_urls:
        try:
            owner, repo, _ = parse_repo_url(url)
        except ValueError:
            mods.append((url, None))
            continue
        ref = identify_repo(owner, repo, modrinth)
        mods.append((repo, ref.project.get("project_id") if ref.project else None))
    resolved = sum(1 for r in first_results if r.success)
    option = _propose(mods, config.loader, config.mc_version, modrinth, resolved,
                      scope="pack", subject="", events=events)
    if option is None or not decide(ChangeTarget(option, config.mc_version, "pack")):
        return None

    check_cancel(cancelled)
    out = target.fallback_output(config.output_dir, option.mc_version)
    retry = ModCompilerConfig(
        mc_version=option.mc_version, loader=config.loader, loader_version="0",
        instance_path=None, github_token=config.github_token, strict_version=False,
        output_dir=str(out), cross_loader=config.cross_loader, docker_test=config.docker_test,
        docker_timeout=config.docker_timeout, prebuild_gate=config.prebuild_gate,
        use_prebuilt=config.use_prebuilt, symbol_check=config.symbol_check)
    official_there = {url for url, (title, _) in zip(repo_urls, mods)
                      if title in option.covered}
    carried = target.carry_over(first_results, retry, config.mc_version, official_there,
                                events=events)
    second = make_pipeline(retry, events)
    second.process_repos(list(repo_urls), resolved=carried)
    return PackRetarget(option, retry, list(second.results), carried,
                        second.generate_report())
