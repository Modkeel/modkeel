"""Recommendation engine for Modkeel.

Queries Modrinth to build a compatibility matrix and recommends the best
MC version + loader combination for a user's installed mods.
"""

import logging
import re
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

import requests

from modkeel.constants import MODRINTH_USER_AGENT
from modkeel.loaders import ALL_LOADERS
from modkeel.models import ModAvailability, RecommendationResult, ScannedMod
from modkeel.utils import fuzzy_score

logger = logging.getLogger("modkeel")

# Modrinth rate limit: 300 req/min. Stay conservative.
_RATE_LIMIT_DELAY = 0.25

# Default loaders to recommend (skip quilt unless user's mods use it)
_DEFAULT_LOADERS = ["neoforge", "forge", "fabric"]

_BASE_URL = "https://api.modrinth.com/v2"
_HEADERS = {"User-Agent": MODRINTH_USER_AGENT}


ProgressCallback = Callable[[int, int, str], None]


class RecommendationEngine:
    """Builds a compatibility matrix and recommends optimal MC version + loader."""

    def __init__(
        self,
        scanned_mods: List[ScannedMod],
        loaders: Optional[List[str]] = None,
        progress_callback: Optional[ProgressCallback] = None,
    ):
        self.scanned_mods = [m for m in scanned_mods if not m.is_library]
        self.loaders = loaders or _DEFAULT_LOADERS
        self.availability: Dict[str, ModAvailability] = {}
        self._progress = progress_callback

    # ── Phase 1: Batch hash identification ───────────────────────────────

    def identify_mods_by_hash(self) -> Dict[str, str]:
        """Use POST /version_files to batch-identify mods by SHA1 hash.

        Returns:
            Dict mapping sha1_hash -> modrinth project_id for matched mods.
        """
        hash_to_mod: Dict[str, ScannedMod] = {}
        for mod in self.scanned_mods:
            hash_to_mod[mod.sha1_hash] = mod

        if not hash_to_mod:
            return {}

        all_hashes = list(hash_to_mod.keys())
        matched: Dict[str, str] = {}

        batch_size = 20
        for i in range(0, len(all_hashes), batch_size):
            batch = all_hashes[i:i + batch_size]
            try:
                resp = requests.post(
                    f"{_BASE_URL}/version_files",
                    json={"hashes": batch, "algorithm": "sha1"},
                    headers=_HEADERS,
                    timeout=15,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for sha1, version_info in data.items():
                        mod = hash_to_mod.get(sha1)
                        if mod:
                            project_id = version_info.get("project_id")
                            if project_id:
                                matched[mod.sha1_hash] = project_id
                time.sleep(_RATE_LIMIT_DELAY)
            except Exception as e:
                logger.debug("Hash batch lookup error: %s", e)

        return matched

    # ── Phase 2: Slug resolution ─────────────────────────────────────────

    def resolve_modrinth_slugs(
        self, hash_matches: Dict[str, str],
    ) -> None:
        """For each scanned mod, find its Modrinth project and populate availability."""
        total = len(self.scanned_mods)

        for idx, mod in enumerate(self.scanned_mods):
            if self._progress:
                self._progress(idx + 1, total, mod.mod_name)

            avail = ModAvailability(
                mod_id=mod.mod_id,
                mod_name=mod.mod_name,
                modrinth_project_id=hash_matches.get(mod.sha1_hash),
            )

            if avail.modrinth_project_id:
                self._fetch_project_details(avail)
            else:
                self._search_modrinth(mod, avail)

            self.availability[mod.mod_id] = avail
            time.sleep(_RATE_LIMIT_DELAY)

    def _fetch_project_details(self, avail: ModAvailability) -> None:
        """Fetch project slug and downloads from project_id."""
        try:
            resp = requests.get(
                f"{_BASE_URL}/project/{avail.modrinth_project_id}",
                headers=_HEADERS,
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                avail.modrinth_slug = data.get("slug")
                avail.downloads = data.get("downloads", 0)
                avail.found_on_modrinth = True
        except Exception as e:
            logger.debug("Project details error for %s: %s",
                         avail.modrinth_project_id, e)

    def _search_modrinth(self, mod: ScannedMod, avail: ModAvailability) -> None:
        """Search Modrinth for a mod by name/id with fuzzy matching."""
        search_query = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", mod.mod_name)

        try:
            facets = '[["project_type:mod"]]'
            resp = requests.get(
                f"{_BASE_URL}/search",
                params={"query": search_query, "facets": facets, "limit": 5},
                headers=_HEADERS,
                timeout=15,
            )
            if resp.status_code != 200:
                return

            hits = resp.json().get("hits", [])
            if not hits:
                return

            best = None
            best_score = 0.0
            for hit in hits:
                slug = hit.get("slug", "")
                title = hit.get("title", "")
                score = max(
                    fuzzy_score(mod.mod_id, slug),
                    fuzzy_score(mod.mod_id, title),
                    fuzzy_score(mod.mod_name, slug),
                    fuzzy_score(mod.mod_name, title),
                )
                if score > best_score:
                    best_score = score
                    best = hit

            if best and best_score < 50.0:
                best = None

            if best:
                avail.modrinth_slug = best["slug"]
                avail.modrinth_project_id = best["project_id"]
                avail.downloads = best.get("downloads", 0)
                avail.found_on_modrinth = True

        except Exception as e:
            logger.debug("Modrinth search error for %s: %s", mod.mod_id, e)

    # ── Phase 3: Version enumeration ─────────────────────────────────────

    def fetch_all_versions(self) -> None:
        """For each mod found on Modrinth, fetch ALL versions to build
        the available_combos set of (mc_version, loader) tuples."""
        modrinth_mods = [
            a for a in self.availability.values() if a.found_on_modrinth
        ]
        total = len(modrinth_mods)

        for idx, avail in enumerate(modrinth_mods):
            if self._progress:
                self._progress(idx + 1, total, avail.mod_name)

            slug = avail.modrinth_slug or avail.modrinth_project_id
            if not slug:
                continue

            try:
                resp = requests.get(
                    f"{_BASE_URL}/project/{slug}/version",
                    headers=_HEADERS,
                    timeout=20,
                )
                if resp.status_code != 200:
                    continue

                versions = resp.json()
                for ver in versions:
                    game_versions = ver.get("game_versions", [])
                    loaders = ver.get("loaders", [])
                    for gv in game_versions:
                        for ldr in loaders:
                            ldr_lower = ldr.lower()
                            if ldr_lower in ALL_LOADERS:
                                avail.available_combos.add((gv, ldr_lower))

                time.sleep(_RATE_LIMIT_DELAY)

            except Exception as e:
                logger.debug("Version fetch error for %s: %s", slug, e)

    # ── Phase 4: Matrix scoring ──────────────────────────────────────────

    def build_recommendations(
        self, mc_version_filter: Optional[str] = None,
    ) -> List[RecommendationResult]:
        """Build and rank (mc_version, loader) recommendations.

        Args:
            mc_version_filter: If set, only consider this MC version family
                (e.g. "1.21" to include 1.21, 1.21.1, 1.21.4).

        Returns:
            Sorted list of RecommendationResult (best first).
        """
        # Collect all combos seen across all mods
        all_combos: Set[Tuple[str, str]] = set()
        for avail in self.availability.values():
            all_combos.update(avail.available_combos)

        # Filter to relevant loaders
        all_combos = {
            (mc, ldr) for mc, ldr in all_combos if ldr in self.loaders
        }

        # Filter MC versions if requested
        if mc_version_filter:
            filter_parts = mc_version_filter.split(".")[:2]
            all_combos = {
                (mc, ldr) for mc, ldr in all_combos
                if mc.split(".")[:2] == filter_parts
            }

        # Sort MC versions descending
        mc_versions = sorted(
            {mc for mc, _ in all_combos},
            key=lambda v: _version_tuple(v),
            reverse=True,
        )

        # Limit to recent versions
        if len(mc_versions) > 20:
            mc_versions = mc_versions[:20]

        # Build recommendations
        all_mod_ids = [m.mod_id for m in self.scanned_mods]
        total_mods = len(all_mod_ids)
        results: List[RecommendationResult] = []

        for mc_ver in mc_versions:
            for loader in self.loaders:
                if (mc_ver, loader) not in all_combos:
                    continue

                available = []
                missing = []
                unknown = []

                for mod_id in all_mod_ids:
                    avail = self.availability.get(mod_id)
                    if not avail or not avail.found_on_modrinth:
                        unknown.append(mod_id)
                    elif (mc_ver, loader) in avail.available_combos:
                        available.append(mod_id)
                    else:
                        missing.append(mod_id)

                known_count = total_mods - len(unknown)
                coverage = (
                    len(available) / known_count * 100
                    if known_count > 0 else 0.0
                )
                total_coverage = (
                    len(available) / total_mods * 100
                    if total_mods > 0 else 0.0
                )

                # Score: coverage is king, version recency as tiebreaker
                score = coverage * 10  # 0-1000
                ver_tuple = _version_tuple(mc_ver)
                score += sum(p * (100 ** (2 - i)) for i, p in enumerate(ver_tuple[:3]))
                if loader == "neoforge":
                    score += 5
                elif loader == "fabric":
                    score += 3

                results.append(RecommendationResult(
                    mc_version=mc_ver,
                    loader=loader,
                    available_mods=available,
                    missing_mods=missing,
                    unknown_mods=unknown,
                    coverage_pct=round(coverage, 1),
                    total_coverage_pct=round(total_coverage, 1),
                    score=score,
                ))

        results.sort(key=lambda r: r.score, reverse=True)
        return results

    # ── Full pipeline ────────────────────────────────────────────────────

    def run(
        self, mc_version_filter: Optional[str] = None,
    ) -> List[RecommendationResult]:
        """Run the full recommendation pipeline.

        1. Identify mods by hash (batch, fast)
        2. Resolve Modrinth slugs for unmatched mods
        3. Fetch all versions for each mod
        4. Build and score recommendations
        """
        hash_matches = self.identify_mods_by_hash()
        self.resolve_modrinth_slugs(hash_matches)
        self.fetch_all_versions()
        return self.build_recommendations(mc_version_filter)


def _version_tuple(v: str) -> Tuple[int, ...]:
    """Convert a version string to a tuple of ints for sorting."""
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)
