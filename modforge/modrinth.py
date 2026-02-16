"""Modrinth API client for ModForge."""

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

import requests

from modforge.constants import MODRINTH_USER_AGENT
from modforge.models import ModCompilerConfig

logger = logging.getLogger("modforge")


class ModrinthClient:
    """Encapsulates Modrinth API interactions."""

    def __init__(self, config: ModCompilerConfig):
        self.config = config

    def is_cross_loader_available(self) -> bool:
        """Check if Sinytra Connector + Forgified Fabric API are available."""
        if hasattr(self, '_cross_loader_available'):
            return self._cross_loader_available

        base_url = "https://api.modrinth.com/v2"
        headers = {"User-Agent": MODRINTH_USER_AGENT}
        mc_version = self.config.mc_version

        available = True
        for slug in ["connector", "forgified-fabric-api"]:
            try:
                resp = requests.get(
                    f"{base_url}/project/{slug}/version",
                    params={
                        "game_versions": f'["{mc_version}"]',
                        "loaders": '["neoforge"]'
                    },
                    headers=headers,
                    timeout=10
                )
                if resp.status_code != 200 or not resp.json():
                    available = False
                    break
            except Exception:
                available = False
                break

        self._cross_loader_available = available
        if not available:
            print(f"  \u26a0\ufe0f  Cross-loader unavailable: Sinytra Connector or "
                  f"Forgified Fabric API not found for MC {mc_version}")
        else:
            print(f"  \u2705 Cross-loader available for MC {mc_version}")
        return available

    def check_modrinth(self, mod_name: str) -> Optional[Dict]:
        """Search Modrinth for a mod matching mod_name + target loader + MC version."""
        base_url = "https://api.modrinth.com/v2"
        headers = {"User-Agent": MODRINTH_USER_AGENT}
        loader = self.config.loader
        mc_version = self.config.mc_version

        loader_facet = loader.lower()

        facets = (
            f'[["categories:{loader_facet}"],'
            f'["versions:{mc_version}"],'
            f'["project_type:mod"]]'
        )

        search_query = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', mod_name)

        try:
            print(f"  \U0001f50d Checking Modrinth for '{mod_name}' "
                  f"({loader} + MC {mc_version})...")
            resp = requests.get(
                f"{base_url}/search",
                params={"query": search_query, "facets": facets, "limit": 5},
                headers=headers,
                timeout=15
            )

            if resp.status_code != 200:
                print(f"    \u26a0\ufe0f  Modrinth search failed: HTTP {resp.status_code}")
                return None

            data = resp.json()
            hits = data.get("hits", [])

            if not hits:
                print(f"    \u2139\ufe0f  Not found on Modrinth")
                return None

            mod_lower = mod_name.lower()
            mod_normalized = re.sub(r'[-_]', '', mod_lower)

            best = None
            for hit in hits:
                slug = hit.get("slug", "").lower()
                title = hit.get("title", "").lower()
                slug_normalized = re.sub(r'[-_]', '', slug)
                title_normalized = re.sub(r'[-_ ]', '', title)
                if (slug == mod_lower or title == mod_lower
                        or slug_normalized == mod_normalized
                        or title_normalized == mod_normalized):
                    best = hit
                    break

            if not best:
                words = search_query.lower().split()
                for hit in hits:
                    title_lower = hit.get("title", "").lower()
                    slug = hit.get("slug", "").lower()
                    slug_normalized = re.sub(r'[-_]', '', slug)
                    if slug_normalized == mod_normalized:
                        best = hit
                        break
                    if len(words) >= 2 and all(w in title_lower for w in words):
                        best = hit
                        break
                    if len(words) == 1 and len(words[0]) >= 4:
                        if (words[0] == slug
                                or title_lower.startswith(words[0])
                                or title_lower.startswith(mod_lower)):
                            best = hit
                            break

            if not best:
                print(f"    \u2139\ufe0f  Modrinth results don't match '{mod_name}'")
                return None

            slug = best["slug"]
            title = best["title"]
            downloads = best.get("downloads", 0)
            print(f"    \u2705 Found on Modrinth: {title} ({slug}) "
                  f"- {downloads:,} downloads")

            ver_resp = requests.get(
                f"{base_url}/project/{slug}/version",
                params={
                    "loaders": f'["{loader_facet}"]',
                    "game_versions": f'["{mc_version}"]'
                },
                headers=headers,
                timeout=15
            )

            if ver_resp.status_code != 200 or not ver_resp.json():
                print(f"    \u26a0\ufe0f  No version files for {loader} + MC {mc_version}")
                return None

            versions = ver_resp.json()
            version_data = None
            for v in versions:
                if v.get("version_type") == "release":
                    version_data = v
                    break
            if not version_data:
                version_data = versions[0]

            files = version_data.get("files", [])
            if not files:
                return None

            primary = next(
                (f for f in files if f.get("primary", False)),
                files[0]
            )

            deps = []
            for dep in version_data.get("dependencies", []):
                if dep.get("dependency_type") == "required":
                    pid = dep.get("project_id")
                    if pid:
                        deps.append(pid)

            result = {
                "slug": slug,
                "title": title,
                "version_number": version_data.get("version_number", "unknown"),
                "version_type": version_data.get("version_type", "release"),
                "download_url": primary["url"],
                "filename": primary["filename"],
                "file_size": primary.get("size", 0),
                "downloads": downloads,
                "required_deps": deps,
            }

            size_mb = result["file_size"] / (1024 * 1024)
            print(f"    \U0001f4e6 Version: {result['version_number']} "
                  f"({result['version_type']}) - {size_mb:.1f} MB")

            return result

        except requests.exceptions.Timeout:
            print(f"    \u26a0\ufe0f  Modrinth search timed out")
            return None
        except Exception as e:
            print(f"    \u26a0\ufe0f  Modrinth search error: {e}")
            return None

    def download_modrinth_deps(
        self, modrinth_result: Dict, _seen: Optional[set] = None,
    ) -> None:
        """Recursively download required dependencies for a Modrinth mod."""
        if _seen is None:
            _seen = set()

        dep_ids = modrinth_result.get("required_deps", [])
        if not dep_ids:
            return

        base_url = "https://api.modrinth.com/v2"
        headers = {"User-Agent": MODRINTH_USER_AGENT}
        loader = self.config.loader.lower()
        mc = self.config.mc_version

        for project_id in dep_ids:
            if project_id in _seen:
                continue
            _seen.add(project_id)

            try:
                pr = requests.get(
                    f"{base_url}/project/{project_id}",
                    headers=headers, timeout=15,
                )
                if not pr.ok:
                    continue
                proj = pr.json()
                slug = proj["slug"]
                title = proj["title"]

                existing = list(
                    self.config.output_dir.glob(f"{slug}*")
                ) + list(
                    self.config.output_dir.glob(
                        f"*{slug.replace('-', '_')}*"
                    )
                )
                if existing:
                    continue

                vr = requests.get(
                    f"{base_url}/project/{slug}/version",
                    params={
                        "loaders": f'["{loader}"]',
                        "game_versions": f'["{mc}"]',
                    },
                    headers=headers, timeout=15,
                )
                if not vr.ok or not vr.json():
                    logger.debug(
                        "No Modrinth version for dep %s (%s + %s)",
                        slug, loader, mc,
                    )
                    continue

                versions = vr.json()
                vdata = next(
                    (v for v in versions
                     if v.get("version_type") == "release"),
                    versions[0],
                )
                files = vdata.get("files", [])
                if not files:
                    continue
                primary = next(
                    (f for f in files if f.get("primary", False)),
                    files[0],
                )

                print(f"    \U0001f4e6 Dependency: {title} "
                      f"v{vdata['version_number']}")
                dl = requests.get(
                    primary["url"], headers=headers, timeout=120,
                )
                dl.raise_for_status()
                fname = primary["filename"]
                dest = self.config.output_dir / fname
                dest.write_bytes(dl.content)
                print(f"    \U0001f4be Saved: {dest}")

                if self.config.mods_path:
                    (self.config.mods_path / fname).write_bytes(
                        dl.content
                    )

                sub_deps = [
                    d.get("project_id")
                    for d in vdata.get("dependencies", [])
                    if d.get("dependency_type") == "required"
                    and d.get("project_id")
                ]
                if sub_deps:
                    self.download_modrinth_deps(
                        {"required_deps": sub_deps}, _seen,
                    )

            except Exception as e:
                logger.debug("Failed to download dep %s: %s",
                             project_id, e)

    def download_modrinth_mod(self, slug: str, mc_version: str,
                              loader: str) -> Optional[Path]:
        """Download the latest version of a mod from Modrinth API."""
        base_url = "https://api.modrinth.com/v2"
        headers = {"User-Agent": MODRINTH_USER_AGENT}

        versions_to_try = [mc_version]
        parts = mc_version.split('.')
        if len(parts) == 3:
            major_minor = f"{parts[0]}.{parts[1]}"
            versions_to_try.append(major_minor)

        for try_version in versions_to_try:
            try:
                url = (f"{base_url}/project/{slug}/version"
                       f"?game_versions=[\"{try_version}\"]"
                       f"&loaders=[\"{loader}\"]")
                resp = requests.get(url, headers=headers, timeout=30)
                if resp.status_code != 200:
                    continue

                versions = resp.json()
                if not versions:
                    continue

                version_data = versions[0]
                files = version_data.get('files', [])
                if not files:
                    continue

                primary = next(
                    (f for f in files if f.get('primary', False)),
                    files[0]
                )
                download_url = primary['url']
                filename = primary['filename']

                print(f"    \U0001f4e5 Downloading {slug}: {filename} "
                      f"(MC {try_version})...")
                dl_resp = requests.get(download_url, timeout=120)
                dl_resp.raise_for_status()

                dest = self.config.output_dir / filename
                dest.write_bytes(dl_resp.content)
                print(f"    \U0001f4be Saved: {dest}")

                if self.config.mods_path:
                    instance_dest = self.config.mods_path / filename
                    instance_dest.write_bytes(dl_resp.content)
                    print(f"    \U0001f4be Installed: {instance_dest}")

                return dest

            except Exception as e:
                logger.debug("Modrinth download error for %s (MC %s): %s",
                             slug, try_version, e)
                continue

        print(f"    \u26a0\ufe0f  Could not download {slug} from Modrinth for "
              f"MC {mc_version}")
        print(f"       Manual download: https://modrinth.com/mod/{slug}")
        return None
