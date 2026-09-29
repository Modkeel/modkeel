"""GitHub API client for Modkeel."""

import base64
import logging
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from modkeel.loaders import get_cross_loader_chain
from modkeel.models import BranchCandidate, ModCompilerConfig

logger = logging.getLogger("modkeel")


def parse_repo_url(url: str) -> Tuple[str, str, Optional[str]]:
    """
    Parse GitHub URL to extract owner, repo, and optional branch.

    Examples:
        https://github.com/PepperCode1/Continuity -> (PepperCode1, Continuity, None)
        https://github.com/PepperCode1/Continuity/tree/1.21.10/dev -> (PepperCode1, Continuity, 1.21.10/dev)
    """
    url = url.strip().rstrip('/')

    # Remove .git suffix if present
    if url.endswith('.git'):
        url = url[:-4]

    # Parse URL
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.split('/') if p]

    if len(path_parts) < 2:
        raise ValueError(f"Invalid GitHub URL: {url}")

    owner = path_parts[0]
    repo = path_parts[1]
    branch = None

    # Check if URL contains /tree/branch
    if len(path_parts) >= 4 and path_parts[2] == 'tree':
        branch = '/'.join(path_parts[3:])

    return owner, repo, branch


class GitHubClient:
    """Encapsulates all GitHub API interactions."""

    def __init__(self, config: ModCompilerConfig):
        self.config = config

    def get_repo_info(self, owner: str, repo: str) -> Optional[Dict]:
        """Fetch repository information from GitHub API."""
        url = f"https://api.github.com/repos/{owner}/{repo}"

        try:
            response = requests.get(url, headers=self.config.github_headers, timeout=10)

            if response.status_code == 404:
                print(f"  \u26a0\ufe0f  Repository not found: {owner}/{repo}")
                return None
            elif response.status_code == 403:
                print(f"  \u26a0\ufe0f  GitHub API rate limit exceeded. Consider using --github-token")
                return None
            elif response.status_code != 200:
                print(f"  \u26a0\ufe0f  GitHub API error: {response.status_code}")
                return None

            return response.json()
        except requests.RequestException as e:
            print(f"  \u26a0\ufe0f  Error fetching repo info: {e}")
            return None

    def search_compatible_repos(self, original_owner: str, original_repo: str,
                                cross_loader_available: bool = False) -> List[Dict]:
        """
        Search for forks and independent ports that might have the target
        Minecraft version.
        """
        print(f"  \U0001f374 Searching for community forks and ports with MC {self.config.mc_version}...")

        searches = [
            f'{original_repo} {self.config.mc_version} {self.config.loader} fork:only',
            f'{original_repo} {self.config.mc_version} fork:only',
            f'{self.config.mc_version} {self.config.loader} {original_repo} fork:only'
        ]

        url = 'https://api.github.com/search/repositories'
        headers = {}
        if self.config.github_token:
            headers['Authorization'] = f'token {self.config.github_token}'

        all_forks = {}

        for query in searches:
            params = {
                'q': query,
                'sort': 'updated',
                'per_page': 10
            }

            try:
                print(f"    \U0001f50e Searching: {query[:60]}...")
                response = requests.get(url, params=params, headers=headers, timeout=15)

                print(f"       Status: {response.status_code}")

                if response.status_code == 403:
                    print(f"       \u26a0\ufe0f  Rate limit hit or forbidden")
                    remaining = response.headers.get('X-RateLimit-Remaining', 'unknown')
                    print(f"       Rate limit remaining: {remaining}")
                    continue

                if response.status_code != 200:
                    print(f"       \u26a0\ufe0f  HTTP {response.status_code}: {response.text[:100]}")
                    continue

                response.raise_for_status()
                results = response.json()

                total_count = results.get('total_count', 0)
                items = results.get('items', [])
                print(f"       Found: {total_count} total, {len(items)} returned")

                for repo_data in items:
                    repo_id = repo_data['id']
                    if repo_id in all_forks:
                        continue

                    repo_name = repo_data.get('full_name', 'unknown')

                    if not repo_data.get('fork'):
                        print(f"       \u26a0\ufe0f  {repo_name}: Not marked as fork")
                        continue

                    parent = repo_data.get('parent', {})
                    if not parent:
                        print(f"       \u26a0\ufe0f  {repo_name}: No parent info (accepting anyway)")
                        fork_repo_name = repo_name.split('/')[-1].lower()
                        orig_lower = original_repo.lower()
                        if fork_repo_name == orig_lower or fork_repo_name.startswith(orig_lower + "-") or fork_repo_name.startswith(orig_lower + "_"):
                            all_forks[repo_id] = repo_data
                            print(f"       \u2705 Fork: {repo_name}")
                        else:
                            print(f"       \u274c {repo_name}: Name mismatch (expected {original_repo}*)")
                        continue

                    parent_full_name = parent.get('full_name', '')
                    original_full = f"{original_owner}/{original_repo}"

                    print(f"       \U0001f50d {repo_name}: parent={parent_full_name}")

                    if original_full.lower() in parent_full_name.lower():
                        all_forks[repo_id] = repo_data
                        print(f"       \u2705 Fork: {repo_name}")
                    else:
                        print(f"       \u274c {repo_name}: Parent mismatch (expected {original_full})")

                time.sleep(0.3)

            except requests.exceptions.Timeout:
                print(f"       \u26a0\ufe0f  Query timed out")
                continue
            except requests.exceptions.RequestException as e:
                print(f"       \u26a0\ufe0f  Request failed: {str(e)[:100]}")
                continue
            except Exception as e:
                print(f"       \u26a0\ufe0f  Unexpected error: {str(e)[:100]}")
                continue

        print(f"    \u2139\ufe0f  Phase 1 found {len(all_forks)} unique forks")

        # Phase 2: Search for independent ports
        print(f"    \U0001f50e Phase 2: Searching independent ports...")
        independent_searches = [
            f'"{original_repo}" {self.config.mc_version} {self.config.loader}',
            f'"{original_repo}" {self.config.loader} port',
            f'"{original_repo}" {self.config.mc_version} port',
        ]

        fallback_loaders = get_cross_loader_chain(self.config.loader)
        if (self.config.cross_loader
                and fallback_loaders
                and cross_loader_available):
            for fallback in fallback_loaders:
                independent_searches.extend([
                    f'"{original_repo}" {self.config.mc_version} {fallback}',
                    f'"{original_repo}" {fallback} port',
                ])

        independent_repos = {}
        original_full = f"{original_owner}/{original_repo}"

        for query in independent_searches:
            params = {
                'q': query,
                'sort': 'updated',
                'per_page': 10
            }

            try:
                print(f"    \U0001f50e Searching: {query[:60]}...")
                response = requests.get(url, params=params, headers=headers, timeout=15)

                if response.status_code == 403:
                    remaining = response.headers.get('X-RateLimit-Remaining', 'unknown')
                    print(f"       \u26a0\ufe0f  Rate limit hit (remaining: {remaining})")
                    continue

                if response.status_code != 200:
                    print(f"       \u26a0\ufe0f  HTTP {response.status_code}: {response.text[:100]}")
                    continue

                results = response.json()
                items = results.get('items', [])
                print(f"       Found: {results.get('total_count', 0)} total, {len(items)} returned")

                for repo_data in items:
                    repo_id = repo_data['id']
                    repo_full_name = repo_data.get('full_name', 'unknown')

                    if repo_id in all_forks:
                        continue
                    if repo_id in independent_repos:
                        continue
                    if repo_full_name.lower() == original_full.lower():
                        continue

                    updated_str = repo_data.get('updated_at', '')
                    if updated_str:
                        updated_at = datetime.strptime(updated_str, '%Y-%m-%dT%H:%M:%SZ')
                        age_days = (datetime.now(timezone.utc).replace(tzinfo=None) - updated_at).days
                        if age_days > 365:
                            continue

                    repo_name_lower = repo_data.get('name', '').lower()
                    description_lower = (repo_data.get('description') or '').lower()
                    mod_name_lower = original_repo.lower()

                    if mod_name_lower not in repo_name_lower and mod_name_lower not in description_lower:
                        continue

                    default_branch = repo_data.get('default_branch', 'main')
                    port_owner = repo_data['owner']['login']
                    port_repo = repo_data['name']
                    gradle_props = self.get_file_from_repo(
                        port_owner, port_repo, default_branch, 'gradle.properties'
                    )
                    if gradle_props is None:
                        print(f"       \u274c {repo_full_name}: No gradle.properties found")
                        continue

                    repo_data['_is_independent_port'] = True
                    independent_repos[repo_id] = repo_data
                    print(f"       \u2705 Independent: {repo_full_name}")

                time.sleep(0.3)

            except requests.exceptions.Timeout:
                print(f"       \u26a0\ufe0f  Query timed out")
                continue
            except requests.exceptions.RequestException as e:
                print(f"       \u26a0\ufe0f  Request failed: {str(e)[:100]}")
                continue
            except Exception as e:
                print(f"       \u26a0\ufe0f  Unexpected error: {str(e)[:100]}")
                continue

        print(f"    \u2139\ufe0f  Phase 2 found {len(independent_repos)} independent ports")

        # Merge all candidates
        all_candidates = {}
        all_candidates.update(all_forks)
        all_candidates.update(independent_repos)

        if not all_candidates:
            print(f"    \u2139\ufe0f  No forks or independent ports found")
            return []

        print(f"    \u2139\ufe0f  Total: {len(all_candidates)} candidates, analyzing...")

        fork_candidates = []

        for repo_data in all_candidates.values():
            fork_full_name = repo_data['full_name']

            if self.config.github_token:
                commit_count = self.get_commit_count(fork_full_name)
                contributor_count = self.get_contributor_count(fork_full_name)
                trust_analysis = self.analyze_contributor_trust(fork_full_name)
            else:
                commit_count = 100
                contributor_count = 1
                trust_analysis = {"trust_score": 50, "warnings": ["No token: trust analysis skipped"], "signals": [], "contributor_count": 1}

            fork_info = {
                'owner': repo_data['owner']['login'],
                'repo': repo_data['name'],
                'full_name': fork_full_name,
                'name': repo_data['name'],
                'description': repo_data.get('description', ''),
                'stars': repo_data.get('stargazers_count', 0),
                'watchers': repo_data.get('watchers_count', 0),
                'forks': repo_data.get('forks_count', 0),
                'updated_at': datetime.strptime(repo_data['updated_at'], '%Y-%m-%dT%H:%M:%SZ'),
                'commit_count': commit_count,
                'contributor_count': contributor_count,
                'topics': repo_data.get('topics', []),
                'url': repo_data['html_url'],
                'trust_analysis': trust_analysis,
                'is_independent_port': repo_data.get('_is_independent_port', False)
            }

            scored = self.score_fork_reliability(fork_info)

            is_independent = repo_data.get('_is_independent_port', False)
            if is_independent:
                scored['score'] -= 10
                scored['signals'].append('independent_port')

            if scored['has_version_match'] and trust_analysis['trust_score'] >= 40:
                fork_candidates.append(scored)

                trust_indicator = "\U0001f512" if trust_analysis['trust_score'] >= 70 else "\u26a0\ufe0f" if trust_analysis['trust_score'] >= 50 else "\U0001f6a8"
                kind = "Independent" if is_independent else "Fork"

                print(f"    \U0001f4e6 {fork_full_name} [{kind}] {trust_indicator}")
                print(f"       Score: {scored['score']}, Trust: {trust_analysis['trust_score']}%, {', '.join(scored['signals'][:3])}")

                if trust_analysis['warnings']:
                    for warning in trust_analysis['warnings'][:2]:
                        print(f"       \u26a0\ufe0f  {warning}")
            elif trust_analysis['trust_score'] < 40:
                print(f"    \U0001f6a8 {fork_full_name} - REJECTED (Trust: {trust_analysis['trust_score']}%)")
                if trust_analysis['warnings']:
                    print(f"       \u26a0\ufe0f  {trust_analysis['warnings'][0]}")

        fork_candidates.sort(key=lambda x: x['score'], reverse=True)

        print(f"  \u2139\ufe0f  {len(fork_candidates)} candidates match criteria")

        return fork_candidates[:5]

    def analyze_contributor_trust(self, full_repo_name: str) -> Dict:
        """Analyze contributors to detect suspicious patterns."""
        try:
            url = f'https://api.github.com/repos/{full_repo_name}/contributors'
            params = {'per_page': 10}

            headers = {}
            if self.config.github_token:
                headers['Authorization'] = f'token {self.config.github_token}'

            response = requests.get(url, params=params, headers=headers, timeout=10)

            if response.status_code != 200:
                return {'trust_score': 50, 'warnings': ['Could not fetch contributors'], 'contributor_count': 0}

            contributors = response.json()

            if not contributors:
                return {'trust_score': 0, 'warnings': ['No contributors found'], 'contributor_count': 0}

            trust_score = 100
            warnings = []
            signals = []

            total_contributors = len(contributors)
            trusted_count = 0
            suspicious_count = 0
            new_accounts = 0

            for contributor in contributors[:5]:
                login = contributor.get('login', '')
                user_data = self.get_user_details(login)

                if user_data:
                    created_at = user_data.get('created_at')
                    if created_at:
                        account_age_days = (datetime.now() - datetime.strptime(created_at, '%Y-%m-%dT%H:%M:%SZ')).days

                        if account_age_days < 90:
                            new_accounts += 1
                            if account_age_days < 30:
                                trust_score -= 10
                                suspicious_count += 1

                    public_repos = user_data.get('public_repos', 0)
                    if public_repos == 0 or public_repos == 1:
                        trust_score -= 5
                        suspicious_count += 1
                    elif public_repos > 5:
                        trusted_count += 1

                    followers = user_data.get('followers', 0)
                    if followers > 10:
                        trusted_count += 1
                    elif followers == 0:
                        suspicious_count += 1

                    has_bio = bool(user_data.get('bio'))
                    has_name = bool(user_data.get('name'))
                    if not has_bio and not has_name:
                        trust_score -= 3

                time.sleep(0.15)

            if total_contributors > 3 and new_accounts >= min(5, total_contributors) * 0.5:
                trust_score -= 20
                warnings.append(f'{new_accounts}/{min(5, total_contributors)} top contributors are new (<3mo)')

            if total_contributors > 3 and suspicious_count >= min(5, total_contributors) * 0.6:
                trust_score -= 15
                warnings.append(f'{suspicious_count}/{min(5, total_contributors)} top contributors look suspicious')

            if trusted_count >= 2:
                trust_score = min(trust_score + 10, 100)
                signals.append(f'{trusted_count} established contributors')

            trust_score = max(0, min(100, trust_score))

            return {
                'trust_score': trust_score,
                'contributor_count': total_contributors,
                'trusted_count': trusted_count,
                'suspicious_count': suspicious_count,
                'new_accounts': new_accounts,
                'warnings': warnings,
                'signals': signals
            }

        except Exception:
            return {
                'trust_score': 50,
                'warnings': [f'Trust analysis error'],
                'contributor_count': 0,
                'signals': []
            }

    def get_user_details(self, username: str) -> Optional[Dict]:
        """Get detailed information about a GitHub user."""
        try:
            url = f'https://api.github.com/users/{username}'

            headers = {}
            if self.config.github_token:
                headers['Authorization'] = f'token {self.config.github_token}'

            response = requests.get(url, headers=headers, timeout=5)

            if response.status_code == 200:
                return response.json()

            return None
        except Exception:
            return None

    def get_contributor_count(self, full_repo_name: str) -> int:
        """Get contributor count for a repository (best effort)."""
        try:
            url = f'https://api.github.com/repos/{full_repo_name}/contributors'
            params = {'per_page': 1, 'anon': 'true'}

            headers = {}
            if self.config.github_token:
                headers['Authorization'] = f'token {self.config.github_token}'

            response = requests.get(url, params=params, headers=headers, timeout=5)

            if 'Link' in response.headers:
                links = response.headers['Link']
                match = re.search(r'page=(\d+)>; rel="last"', links)
                if match:
                    return int(match.group(1))

            return 1
        except Exception:
            return 0

    def get_commit_count(self, full_repo_name: str) -> int:
        """Get approximate commit count for a repository."""
        try:
            url = f'https://api.github.com/repos/{full_repo_name}/commits'
            params = {'per_page': 1}

            headers = {}
            if self.config.github_token:
                headers['Authorization'] = f'token {self.config.github_token}'

            response = requests.get(url, params=params, headers=headers)

            if 'Link' in response.headers:
                links = response.headers['Link']
                match = re.search(r'page=(\d+)>; rel="last"', links)
                if match:
                    return int(match.group(1))

            return 50
        except Exception:
            return 0

    def score_fork_reliability(self, fork_data: Dict) -> Dict:
        """Score a fork based on multiple reliability signals."""
        score = 0
        signals = []

        name = fork_data['name'].lower()
        description = (fork_data.get('description') or '').lower()
        topics = [t.lower() for t in fork_data.get('topics', [])]

        target_version = self.config.mc_version.lower()
        target_loader = self.config.loader.lower()

        ver_escaped = re.escape(target_version)
        version_pattern = re.compile(rf'(?<![.\d]){ver_escaped}(?![.\d])')
        loader_pattern = re.compile(rf'(?<!\w){re.escape(target_loader)}(?!\w)')

        has_version_match = False
        version_location = []

        if version_pattern.search(name):
            score += 60
            has_version_match = True
            version_location.append("name")
        elif version_pattern.search(description):
            score += 50
            has_version_match = True
            version_location.append("desc")
        elif any(version_pattern.search(topic) for topic in topics):
            score += 40
            has_version_match = True
            version_location.append("topics")

        if version_location:
            signals.append(f"Ver:{'+'.join(version_location)}")

        has_loader_match = False
        loader_location = []

        if loader_pattern.search(name):
            score += 40
            has_loader_match = True
            loader_location.append("name")
        elif loader_pattern.search(description):
            score += 30
            has_loader_match = True
            loader_location.append("desc")
        elif any(loader_pattern.search(topic) for topic in topics):
            score += 20
            has_loader_match = True
            loader_location.append("topics")

        if loader_location:
            signals.append(f"Loader:{'+'.join(loader_location)}")

        trust_analysis = fork_data.get('trust_analysis', {})
        trust_score = trust_analysis.get('trust_score', 50)
        trust_points = int(trust_score / 2)
        score += trust_points

        if trust_score >= 80:
            signals.append(f"Trust:HIGH({trust_score}%)")
        elif trust_score >= 60:
            signals.append(f"Trust:OK({trust_score}%)")
        elif trust_score >= 40:
            signals.append(f"Trust:LOW({trust_score}%)")
        else:
            signals.append(f"Trust:CRITICAL({trust_score}%)")

        if trust_analysis.get('warnings'):
            signals.extend([f"\u26a0\ufe0f{w[:30]}" for w in trust_analysis['warnings'][:1]])

        days_since_update = (datetime.now() - fork_data['updated_at']).days
        if days_since_update < 30:
            score += 40
            signals.append("Updated:<1mo")
        elif days_since_update < 90:
            score += 20
            signals.append("Updated:<3mo")
        elif days_since_update < 180:
            score += 10
            signals.append("Updated:<6mo")

        commit_points = min(fork_data['commit_count'] // 100, 40)
        score += commit_points
        if fork_data['commit_count'] > 0:
            if fork_data['commit_count'] >= 1000:
                signals.append(f"{fork_data['commit_count']//1000}k commits")
            else:
                signals.append(f"{fork_data['commit_count']} commits")

        if fork_data.get('contributor_count', 0) > 0:
            contributor_points = min(fork_data['contributor_count'] * 5, 20)
            score += contributor_points
            if fork_data['contributor_count'] > 1:
                signals.append(f"{fork_data['contributor_count']} contributors")

        if fork_data['stars'] > 0:
            star_points = min(fork_data['stars'] * 2, 20)
            score += star_points
            signals.append(f"{fork_data['stars']}\u2b50")

        if fork_data.get('watchers', 0) > 0:
            watcher_points = min(fork_data['watchers'], 10)
            score += watcher_points

        if fork_data.get('forks', 0) > 0:
            score += 5
            signals.append(f"{fork_data['forks']} forks")

        minecraft_topics = ['minecraft', 'mod', 'minecraft-mod', 'minecraft-mods']
        if any(topic in topics for topic in minecraft_topics):
            score += 10

        return {
            'score': score,
            'signals': signals,
            'fork': fork_data,
            'has_version_match': has_version_match,
            'has_loader_match': has_loader_match,
            'trust_score': trust_score
        }

    def get_branches(self, owner: str, repo: str) -> List[BranchCandidate]:
        """Fetch all branches from a repository."""
        url = f"https://api.github.com/repos/{owner}/{repo}/branches"
        branches = []

        try:
            response = requests.get(url, headers=self.config.github_headers, timeout=10)

            if response.status_code != 200:
                print(f"  \u26a0\ufe0f  Could not fetch branches: HTTP {response.status_code}")
                return branches

            branch_data = response.json()

            for branch in branch_data:
                commit_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{branch['commit']['sha']}"
                commit_response = requests.get(commit_url, headers=self.config.github_headers, timeout=10)

                commit_date = ""
                if commit_response.status_code == 200:
                    commit_date = commit_response.json()['commit']['committer']['date']

                candidate = BranchCandidate(
                    name=branch['name'],
                    commit_sha=branch['commit']['sha'],
                    commit_date=commit_date
                )
                branches.append(candidate)

            return branches

        except requests.RequestException as e:
            print(f"  \u26a0\ufe0f  Error fetching branches: {e}")
            return branches

    def get_tree(self, owner: str, repo: str, branch: str) -> Optional[List[str]]:
        """List every file path in a branch with one API call (None on failure)."""
        url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}"
        try:
            response = requests.get(url, headers=self.config.github_headers,
                                    params={"recursive": "1"}, timeout=15)
        except requests.RequestException:
            return None
        if response.status_code != 200:
            return None
        return [e["path"] for e in response.json().get("tree", []) if e.get("type") == "blob"]

    def get_raw_file(self, owner: str, repo: str, branch: str, path: str) -> Optional[str]:
        """Fetch one file at an exact path via raw.githubusercontent (no API quota)."""
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
        try:
            response = requests.get(url, timeout=10)
        except requests.RequestException:
            return None
        return response.text if response.status_code == 200 else None

    def get_file_from_repo(self, owner: str, repo: str, branch: str,
                           filepath: str) -> Optional[str]:
        """
        Fetch a single file from a GitHub repository without cloning.
        """
        common_paths = [
            filepath,
            f"common/{filepath}",
            f"fabric/{filepath}",
            f"neoforge/{filepath}",
            f"forge/{filepath}",
        ]

        for path in common_paths:
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"

            try:
                response = requests.get(raw_url, timeout=5)
                if response.status_code == 200:
                    return response.text
            except requests.RequestException:
                pass

            api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={branch}"

            try:
                response = requests.get(api_url, headers=self.config.github_headers, timeout=5)
                if response.status_code == 200:
                    data = response.json()
                    content = base64.b64decode(data['content']).decode('utf-8')
                    return content
            except requests.RequestException:
                pass
            except (KeyError, ValueError):
                pass

        return None
