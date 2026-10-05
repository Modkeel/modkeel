"""Tests for modkeel.recommend module."""

import unittest
from unittest.mock import MagicMock, patch

from modkeel.models import ModAvailability, ScannedMod
from modkeel.recommend import RecommendationEngine, _version_tuple


def _make_mod(mod_id: str, mod_name: str = "", sha1: str = "abc123",
              is_library: bool = False) -> ScannedMod:
    """Create a ScannedMod for testing."""
    return ScannedMod(
        jar_path=f"/mods/{mod_id}.jar",
        jar_filename=f"{mod_id}.jar",
        mod_id=mod_id,
        mod_name=mod_name or mod_id.capitalize(),
        mod_version="1.0.0",
        declared_loader="neoforge",
        declared_mc_range="[1.21,1.22)",
        declared_mc_version="1.21",
        sha1_hash=sha1,
        is_library=is_library,
    )


class TestVersionTuple(unittest.TestCase):
    """Test version string to tuple conversion."""

    def test_three_part(self):
        self.assertEqual(_version_tuple("1.21.4"), (1, 21, 4))

    def test_two_part(self):
        self.assertEqual(_version_tuple("1.21"), (1, 21))

    def test_one_part(self):
        self.assertEqual(_version_tuple("1"), (1,))

    def test_non_numeric(self):
        self.assertEqual(_version_tuple("1.21.a"), (1, 21, 0))


class TestEngineInit(unittest.TestCase):
    """Test RecommendationEngine initialization."""

    def test_filters_libraries(self):
        mods = [
            _make_mod("create", sha1="aaa"),
            _make_mod("fabric-api", is_library=True, sha1="bbb"),
            _make_mod("jei", sha1="ccc"),
        ]
        engine = RecommendationEngine(mods)
        self.assertEqual(len(engine.scanned_mods), 2)
        ids = {m.mod_id for m in engine.scanned_mods}
        self.assertEqual(ids, {"create", "jei"})

    def test_default_loaders(self):
        engine = RecommendationEngine([])
        self.assertIn("neoforge", engine.loaders)
        self.assertIn("fabric", engine.loaders)

    def test_custom_loaders(self):
        engine = RecommendationEngine([], loaders=["fabric"])
        self.assertEqual(engine.loaders, ["fabric"])


class TestIdentifyModsByHash(unittest.TestCase):
    """Test hash-based mod identification."""

    @patch("modkeel.recommend.requests.post")
    def test_batch_identification(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "hash1": {"project_id": "proj_create"},
        }
        mock_post.return_value = mock_resp

        mods = [_make_mod("create", sha1="hash1")]
        engine = RecommendationEngine(mods)
        result = engine.identify_mods_by_hash()

        self.assertEqual(result, {"hash1": "proj_create"})
        mock_post.assert_called_once()

    @patch("modkeel.recommend.requests.post")
    def test_empty_mods(self, mock_post):
        engine = RecommendationEngine([])
        result = engine.identify_mods_by_hash()
        self.assertEqual(result, {})
        mock_post.assert_not_called()

    @patch("modkeel.recommend.requests.post")
    def test_api_error_handled(self, mock_post):
        mock_post.side_effect = Exception("Network error")
        mods = [_make_mod("create", sha1="hash1")]
        engine = RecommendationEngine(mods)
        result = engine.identify_mods_by_hash()
        self.assertEqual(result, {})


class TestResolveModrinthSlugs(unittest.TestCase):
    """Test Modrinth slug resolution."""

    @patch("modkeel.recommend.requests.get")
    @patch("modkeel.recommend.time.sleep")
    def test_hash_matched_fetches_details(self, _sleep, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "slug": "create",
            "downloads": 50000,
        }
        mock_get.return_value = mock_resp

        mods = [_make_mod("create", sha1="hash1")]
        engine = RecommendationEngine(mods)
        engine.resolve_modrinth_slugs({"hash1": "proj_create"})

        avail = engine.availability["create"]
        self.assertTrue(avail.found_on_modrinth)
        self.assertEqual(avail.modrinth_slug, "create")
        self.assertEqual(avail.downloads, 50000)

    @patch("modkeel.recommend.requests.get")
    @patch("modkeel.recommend.time.sleep")
    def test_fuzzy_search_fallback(self, _sleep, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "hits": [
                {"slug": "jei", "title": "JEI", "project_id": "proj_jei",
                 "downloads": 10000},
            ],
        }
        mock_get.return_value = mock_resp

        mods = [_make_mod("jei", "JEI", sha1="hash2")]
        engine = RecommendationEngine(mods)
        engine.resolve_modrinth_slugs({})

        avail = engine.availability["jei"]
        self.assertTrue(avail.found_on_modrinth)
        self.assertEqual(avail.modrinth_slug, "jei")

    @patch("modkeel.recommend.requests.get")
    @patch("modkeel.recommend.time.sleep")
    def test_not_found_on_modrinth(self, _sleep, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"hits": []}
        mock_get.return_value = mock_resp

        mods = [_make_mod("custom_mod", sha1="hash3")]
        engine = RecommendationEngine(mods)
        engine.resolve_modrinth_slugs({})

        avail = engine.availability["custom_mod"]
        self.assertFalse(avail.found_on_modrinth)


class TestBuildRecommendations(unittest.TestCase):
    """Test recommendation matrix scoring."""

    def _engine_with_availability(self, mods, avail_data):
        """Create an engine with pre-populated availability."""
        engine = RecommendationEngine(mods)
        for mod_id, combos, found in avail_data:
            engine.availability[mod_id] = ModAvailability(
                mod_id=mod_id,
                mod_name=mod_id.capitalize(),
                available_combos=set(combos),
                found_on_modrinth=found,
            )
        return engine

    def test_full_coverage_scores_highest(self):
        mods = [
            _make_mod("create", sha1="a"),
            _make_mod("jei", sha1="b"),
        ]
        avail = [
            ("create", [("1.21.4", "neoforge"), ("1.21.4", "fabric")], True),
            ("jei", [("1.21.4", "neoforge"), ("1.21.1", "fabric")], True),
        ]
        engine = self._engine_with_availability(mods, avail)
        results = engine.build_recommendations()

        # 1.21.4 + neoforge should have 100% coverage (both mods)
        best = results[0]
        self.assertEqual(best.mc_version, "1.21.4")
        self.assertEqual(best.loader, "neoforge")
        self.assertEqual(best.coverage_pct, 100.0)

    def test_partial_coverage(self):
        mods = [
            _make_mod("create", sha1="a"),
            _make_mod("jei", sha1="b"),
            _make_mod("sodium", sha1="c"),
        ]
        avail = [
            ("create", [("1.21.4", "neoforge")], True),
            ("jei", [("1.21.4", "neoforge")], True),
            ("sodium", [("1.21.4", "fabric")], True),
        ]
        engine = self._engine_with_availability(mods, avail)
        results = engine.build_recommendations()

        # Best neoforge result should have 2/3 coverage
        nf_results = [r for r in results if r.loader == "neoforge"
                       and r.mc_version == "1.21.4"]
        self.assertEqual(len(nf_results), 1)
        self.assertAlmostEqual(nf_results[0].coverage_pct, 66.7, places=0)

    def test_unknown_mods_classified(self):
        mods = [
            _make_mod("create", sha1="a"),
            _make_mod("custom", sha1="b"),
        ]
        avail = [
            ("create", [("1.21.4", "neoforge")], True),
            ("custom", [], False),  # Not on Modrinth
        ]
        engine = self._engine_with_availability(mods, avail)
        results = engine.build_recommendations()

        best = results[0]
        self.assertIn("create", best.available_mods)
        self.assertIn("custom", best.unknown_mods)
        # Coverage should be 100% (1/1 known mod)
        self.assertEqual(best.coverage_pct, 100.0)
        # Total coverage should be 50% (1/2 total mods)
        self.assertEqual(best.total_coverage_pct, 50.0)

    def test_mc_version_filter(self):
        mods = [_make_mod("create", sha1="a")]
        avail = [
            ("create", [("1.21.4", "neoforge"), ("1.20.1", "neoforge")], True),
        ]
        engine = self._engine_with_availability(mods, avail)
        results = engine.build_recommendations(mc_version_filter="1.21")

        mc_versions = {r.mc_version for r in results}
        self.assertIn("1.21.4", mc_versions)
        self.assertNotIn("1.20.1", mc_versions)

    def test_empty_mods(self):
        engine = RecommendationEngine([])
        results = engine.build_recommendations()
        self.assertEqual(results, [])

    def test_newer_version_preferred_on_tie(self):
        mods = [_make_mod("create", sha1="a")]
        avail = [
            ("create", [("1.21.4", "neoforge"), ("1.21.1", "neoforge")], True),
        ]
        engine = self._engine_with_availability(mods, avail)
        results = engine.build_recommendations()

        # Both have 100% coverage, but 1.21.4 should rank higher
        self.assertEqual(results[0].mc_version, "1.21.4")
        self.assertEqual(results[1].mc_version, "1.21.1")

    def test_missing_mods_listed(self):
        mods = [
            _make_mod("create", sha1="a"),
            _make_mod("jei", sha1="b"),
        ]
        avail = [
            ("create", [("1.21.4", "neoforge")], True),
            ("jei", [("1.21.1", "neoforge")], True),  # Not for 1.21.4
        ]
        engine = self._engine_with_availability(mods, avail)
        results = engine.build_recommendations()

        rec_1214 = [r for r in results if r.mc_version == "1.21.4"
                     and r.loader == "neoforge"][0]
        self.assertIn("jei", rec_1214.missing_mods)


class TestFullPipeline(unittest.TestCase):
    """Test the full recommendation pipeline."""

    @patch("modkeel.recommend.requests.get")
    @patch("modkeel.recommend.requests.post")
    @patch("modkeel.recommend.time.sleep")
    def test_run_end_to_end(self, _sleep, mock_post, mock_get):
        # Hash lookup returns no matches
        hash_resp = MagicMock()
        hash_resp.status_code = 200
        hash_resp.json.return_value = {}
        mock_post.return_value = hash_resp

        # Search returns a match, version fetch returns versions
        def get_side_effect(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "/search" in url:
                resp.json.return_value = {
                    "hits": [
                        {"slug": "create", "title": "Create",
                         "project_id": "p1", "downloads": 5000},
                    ],
                }
            elif "/version" in url:
                resp.json.return_value = [
                    {"game_versions": ["1.21.4"], "loaders": ["neoforge", "fabric"]},
                    {"game_versions": ["1.21.1"], "loaders": ["neoforge"]},
                ]
            else:
                resp.json.return_value = {}
            return resp

        mock_get.side_effect = get_side_effect

        mods = [_make_mod("create", "Create", sha1="hash1")]
        engine = RecommendationEngine(mods)
        results = engine.run()

        self.assertTrue(len(results) > 0)
        best = results[0]
        self.assertIn("create", best.available_mods)
        self.assertGreater(best.coverage_pct, 0)


if __name__ == "__main__":
    unittest.main()
