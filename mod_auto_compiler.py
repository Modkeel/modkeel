#!/usr/bin/env python3
"""
Minecraft Mod Auto-Compiler
Automatically detects, compiles, and installs mods from GitHub repositories
for specific Minecraft versions and mod loaders.

Author: Juan - AutoKufe
"""

import argparse
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse
import time

# ============================================================================
# CONSTANTS
# ============================================================================
MODRINTH_USER_AGENT = "ModForge/1.0 (github.com/juanzab/ModForge)"

# ============================================================================
# LOGGING SETUP
# ============================================================================
logger = logging.getLogger("modforge")


def setup_logging(log_file: Optional[str] = None) -> None:
    """Configure logging with stdout and optional file output."""
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console handler (always)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler (optional)
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        print(f"Logging to file: {log_file}")

try:
    import requests
except ImportError:
    print("Error: 'requests' library not found. Install it with: pip install requests")
    sys.exit(1)

try:
    import toml
except ImportError:
    print("Error: 'toml' library not found. Install it with: pip install toml")
    sys.exit(1)

# ============================================================================
# WINDOWS EMOJI COMPATIBILITY FIX
# ============================================================================
import platform

def setup_windows_console():
    """Setup console for emoji support on Windows"""
    if platform.system() == 'Windows':
        # Try to enable UTF-8 output
        try:
            import sys
            import codecs
            sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
            sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')
        except:
            # If UTF-8 doesn't work, replace print with safe version
            import builtins
            original_print = builtins.print
            
            def safe_print(*args, **kwargs):
                try:
                    original_print(*args, **kwargs)
                except UnicodeEncodeError:
                    # Replace emojis with ASCII
                    safe_args = []
                    for arg in args:
                        if isinstance(arg, str):
                            replacements = {
                                '📦': '[PKG]', '🔍': '[SEARCH]', '✅': '[OK]',
                                '❌': '[FAIL]', '⚠️': '[WARN]', '🌿': '[BRANCH]',
                                '📥': '[DOWN]', '🔨': '[BUILD]', '📋': '[LIST]',
                                '🎯': '[TARGET]', '⭐': '*', '🍴': '[FORK]',
                                '🔒': '[LOCK]', '🚨': '[ALERT]', '📊': '[STAT]',
                                '📍': '[LOC]', '💾': '[SAVE]', '🔎': '[FIND]',
                            }
                            for emoji, ascii_rep in replacements.items():
                                arg = arg.replace(emoji, ascii_rep)
                            safe_args.append(arg)
                        else:
                            safe_args.append(arg)
                    original_print(*safe_args, **kwargs)
            
            builtins.print = safe_print

setup_windows_console()
# ============================================================================


class ModCompilerConfig:
    """Configuration for the mod compilation process"""

    def __init__(self, mc_version: str, loader: str, loader_version: str,
                 instance_path: Optional[str] = None, github_token: Optional[str] = None,
                 strict_version: bool = False, output_dir: str = "out",
                 cross_loader: bool = True):
        self.mc_version = mc_version
        self.loader = loader.lower()
        self.loader_version = loader_version
        self.github_token = github_token
        self.strict_version = strict_version
        self.cross_loader = cross_loader

        # Output directory (always used)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Instance path (optional - if provided, JARs also go to instance/mods/)
        if instance_path:
            self.instance_path = Path(instance_path)
            if not self.instance_path.exists():
                raise ValueError(f"Instance path does not exist: {instance_path}")
            self.mods_path = self.instance_path / "mods"
            self.mods_path.mkdir(exist_ok=True)
        else:
            self.instance_path = None
            self.mods_path = None

        # GitHub API headers
        self.github_headers = {
            "Accept": "application/vnd.github.v3+json"
        }
        if github_token:
            self.github_headers["Authorization"] = f"token {github_token}"


class BranchCandidate:
    """Represents a potential branch for compilation"""
    
    def __init__(self, name: str, commit_sha: str, commit_date: str):
        self.name = name
        self.commit_sha = commit_sha
        self.commit_date = commit_date
        self.score = 0  # Will be calculated based on relevance
        
        # Pre-validation fields (populated via GitHub API)
        self.minecraft_version = None
        self.loader = None
        self.loader_version = None
        self.is_compatible = False
        self.validation_error = None
        
        # Metadata validation fields
        self.version_range = None  # e.g., "[1.21,1.22)" or "~1.21.0"
        self.validation_method = None  # 'metadata_range' or 'gradle_properties'
    
    def __repr__(self):
        return f"BranchCandidate(name={self.name}, mc={self.minecraft_version}, range={self.version_range}, loader={self.loader}, compatible={self.is_compatible}, score={self.score})"


class FailureType(Enum):
    """Classification of build failures for dependency-aware retries."""
    NONE = "none"
    DEPENDENCY_RESOLUTION = "dependency_resolution"
    BUILD_ERROR = "build_error"
    CLONE_ERROR = "clone_error"
    VALIDATION_ERROR = "validation_error"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class CompilationResult:
    """Result of attempting to compile a mod"""

    def __init__(self, repo_url: str, success: bool, branch: Optional[str] = None,
                 jar_path: Optional[str] = None, error: Optional[str] = None,
                 mod_name: Optional[str] = None, mod_version: Optional[str] = None,
                 compiled_mc_version: Optional[str] = None,
                 failure_type: "FailureType" = None,
                 missing_dependencies: Optional[List[str]] = None,
                 clone_dir: Optional[Path] = None,
                 is_cross_loader: bool = False,
                 modrinth_download: bool = False):
        self.repo_url = repo_url
        self.success = success
        self.branch = branch
        self.jar_path = jar_path
        self.error = error
        self.mod_name = mod_name
        self.mod_version = mod_version
        self.compiled_mc_version = compiled_mc_version
        self.failure_type = failure_type or FailureType.NONE
        self.missing_dependencies = missing_dependencies or []
        self.clone_dir = clone_dir
        self.is_cross_loader = is_cross_loader
        self.modrinth_download = modrinth_download


class ModAutoCompiler:
    """Main class for automatic mod compilation"""
    
    def __init__(self, config: ModCompilerConfig):
        self.config = config
        self.results: List[CompilationResult] = []
        self.temp_dir = None
    
    def parse_repo_url(self, url: str) -> Tuple[str, str, Optional[str]]:
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
    
    def get_repo_info(self, owner: str, repo: str) -> Optional[Dict]:
        """
        Fetch repository information from GitHub API including stars, forks, etc.
        """
        url = f"https://api.github.com/repos/{owner}/{repo}"
        
        try:
            response = requests.get(url, headers=self.config.github_headers, timeout=10)
            
            if response.status_code == 404:
                print(f"  ⚠️  Repository not found: {owner}/{repo}")
                return None
            elif response.status_code == 403:
                print(f"  ⚠️  GitHub API rate limit exceeded. Consider using --github-token")
                return None
            elif response.status_code != 200:
                print(f"  ⚠️  GitHub API error: {response.status_code}")
                return None
            
            return response.json()
        except requests.RequestException as e:
            print(f"  ⚠️  Error fetching repo info: {e}")
            return None
    
    def search_compatible_repos(self, original_owner: str, original_repo: str) -> List[Dict]:
        """
        Search for forks and independent ports that might have the target
        Minecraft version. Checks: repo name, description, and topics for
        version + loader.
        Returns scored candidates ordered by reliability score.
        """
        print(f"  🍴 Searching for community forks and ports with MC {self.config.mc_version}...")
        
        # Build multiple search queries to cast a wider net
        searches = [
            # Query 1: Version + Loader + Repo name
            f'{original_repo} {self.config.mc_version} {self.config.loader} fork:only',
            # Query 2: Version + Repo name (loader might be in description)
            f'{original_repo} {self.config.mc_version} fork:only',
            # Query 3: Just version (if repo is obscure)
            f'{self.config.mc_version} {self.config.loader} {original_repo} fork:only'
        ]
        
        url = 'https://api.github.com/search/repositories'
        headers = {}
        if self.config.github_token:
            headers['Authorization'] = f'token {self.config.github_token}'
        
        all_forks = {}  # Use dict to deduplicate
        
        for query in searches:
            params = {
                'q': query,
                'sort': 'updated',  # Most recently updated
                'per_page': 10  # Top 10 per query
            }
            
            try:
                print(f"    🔎 Searching: {query[:60]}...")
                response = requests.get(url, params=params, headers=headers, timeout=15)
                
                print(f"       Status: {response.status_code}")
                
                if response.status_code == 403:
                    print(f"       ⚠️  Rate limit hit or forbidden")
                    remaining = response.headers.get('X-RateLimit-Remaining', 'unknown')
                    print(f"       Rate limit remaining: {remaining}")
                    continue
                
                if response.status_code != 200:
                    print(f"       ⚠️  HTTP {response.status_code}: {response.text[:100]}")
                    continue
                
                response.raise_for_status()
                results = response.json()
                
                total_count = results.get('total_count', 0)
                items = results.get('items', [])
                print(f"       Found: {total_count} total, {len(items)} returned")
                
                for repo_data in items:
                    # Skip if already found
                    repo_id = repo_data['id']
                    if repo_id in all_forks:
                        continue
                    
                    repo_name = repo_data.get('full_name', 'unknown')
                    
                    # Verify it's actually a fork of the original repo
                    if not repo_data.get('fork'):
                        print(f"       ⚠️  {repo_name}: Not marked as fork")
                        continue
                    
                    parent = repo_data.get('parent', {})
                    if not parent:
                        print(f"       ⚠️  {repo_name}: No parent info (accepting anyway)")
                        # Accept if repo name starts with original name
                        # e.g. "Create-1.21.10" starts with "Create"
                        fork_repo_name = repo_name.split('/')[-1].lower()
                        orig_lower = original_repo.lower()
                        if fork_repo_name == orig_lower or fork_repo_name.startswith(orig_lower + "-") or fork_repo_name.startswith(orig_lower + "_"):
                            all_forks[repo_id] = repo_data
                            print(f"       ✅ Fork: {repo_name}")
                        else:
                            print(f"       ❌ {repo_name}: Name mismatch (expected {original_repo}*)")
                        continue
                    
                    parent_full_name = parent.get('full_name', '')
                    
                    # Check if it's a fork of our target repo
                    original_full = f"{original_owner}/{original_repo}"
                    
                    print(f"       🔍 {repo_name}: parent={parent_full_name}")
                    
                    if original_full.lower() in parent_full_name.lower():
                        all_forks[repo_id] = repo_data
                        print(f"       ✅ Fork: {repo_name}")
                    else:
                        print(f"       ❌ {repo_name}: Parent mismatch (expected {original_full})")
                
                # Small delay between queries
                time.sleep(0.3)
                
            except requests.exceptions.Timeout:
                print(f"       ⚠️  Query timed out")
                continue
            except requests.exceptions.RequestException as e:
                print(f"       ⚠️  Request failed: {str(e)[:100]}")
                continue
            except Exception as e:
                print(f"       ⚠️  Unexpected error: {str(e)[:100]}")
                continue
        
        print(f"    ℹ️  Phase 1 found {len(all_forks)} unique forks")

        # Phase 2: Search for independent ports (not GitHub forks)
        print(f"    🔎 Phase 2: Searching independent ports...")
        independent_searches = [
            f'"{original_repo}" {self.config.mc_version} {self.config.loader}',
            f'"{original_repo}" {self.config.loader} port',
            f'"{original_repo}" {self.config.mc_version} port',
        ]

        # Cross-loader: also search with fabric when targeting neoforge/forge
        if (self.config.cross_loader
                and self.config.loader in ("neoforge", "forge")
                and self.is_cross_loader_available()):
            independent_searches.extend([
                f'"{original_repo}" {self.config.mc_version} fabric',
                f'"{original_repo}" fabric port',
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
                print(f"    🔎 Searching: {query[:60]}...")
                response = requests.get(url, params=params, headers=headers, timeout=15)

                if response.status_code == 403:
                    remaining = response.headers.get('X-RateLimit-Remaining', 'unknown')
                    print(f"       ⚠️  Rate limit hit (remaining: {remaining})")
                    continue

                if response.status_code != 200:
                    print(f"       ⚠️  HTTP {response.status_code}: {response.text[:100]}")
                    continue

                results = response.json()
                items = results.get('items', [])
                print(f"       Found: {results.get('total_count', 0)} total, {len(items)} returned")

                for repo_data in items:
                    repo_id = repo_data['id']
                    repo_full_name = repo_data.get('full_name', 'unknown')

                    # Skip if already found as fork
                    if repo_id in all_forks:
                        continue
                    # Skip if already found as independent
                    if repo_id in independent_repos:
                        continue
                    # Skip the original repo
                    if repo_full_name.lower() == original_full.lower():
                        continue

                    # Skip repos not updated in >1 year
                    updated_str = repo_data.get('updated_at', '')
                    if updated_str:
                        updated_at = datetime.strptime(updated_str, '%Y-%m-%dT%H:%M:%SZ')
                        age_days = (datetime.now(timezone.utc).replace(tzinfo=None) - updated_at).days
                        if age_days > 365:
                            continue

                    # Verify name or description mentions the original mod
                    repo_name_lower = repo_data.get('name', '').lower()
                    description_lower = (repo_data.get('description') or '').lower()
                    mod_name_lower = original_repo.lower()

                    if mod_name_lower not in repo_name_lower and mod_name_lower not in description_lower:
                        continue

                    # Optional: verify via gradle.properties that it's a real port
                    default_branch = repo_data.get('default_branch', 'main')
                    port_owner = repo_data['owner']['login']
                    port_repo = repo_data['name']
                    gradle_props = self.get_file_from_repo(
                        port_owner, port_repo, default_branch, 'gradle.properties'
                    )
                    if gradle_props is None:
                        # No gradle.properties = probably not a mod project
                        print(f"       ❌ {repo_full_name}: No gradle.properties found")
                        continue

                    # Mark as independent port
                    repo_data['_is_independent_port'] = True
                    independent_repos[repo_id] = repo_data
                    print(f"       ✅ Independent: {repo_full_name}")

                time.sleep(0.3)

            except requests.exceptions.Timeout:
                print(f"       ⚠️  Query timed out")
                continue
            except requests.exceptions.RequestException as e:
                print(f"       ⚠️  Request failed: {str(e)[:100]}")
                continue
            except Exception as e:
                print(f"       ⚠️  Unexpected error: {str(e)[:100]}")
                continue

        print(f"    ℹ️  Phase 2 found {len(independent_repos)} independent ports")

        # Merge all candidates
        all_candidates = {}
        all_candidates.update(all_forks)
        all_candidates.update(independent_repos)

        if not all_candidates:
            print(f"    ℹ️  No forks or independent ports found")
            return []

        print(f"    ℹ️  Total: {len(all_candidates)} candidates, analyzing...")

        # Score and filter
        fork_candidates = []

        for repo_data in all_candidates.values():
            fork_full_name = repo_data['full_name']
            
            # Get real commit and contributor counts (conditional on token)
            if self.config.github_token:
                commit_count = self.get_commit_count(fork_full_name)
                contributor_count = self.get_contributor_count(fork_full_name)
                trust_analysis = self.analyze_contributor_trust(fork_full_name)
            else:
                commit_count = 100  # Estimate when no token
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

            # Score this fork/port
            scored = self.score_fork_reliability(fork_info)

            # Apply penalty for independent ports (no verified parent repo)
            is_independent = repo_data.get('_is_independent_port', False)
            if is_independent:
                scored['score'] -= 10
                scored['signals'].append('independent_port')

            # Require version match as minimum; loader match alone is not enough
            if scored['has_version_match'] and trust_analysis['trust_score'] >= 40:
                fork_candidates.append(scored)

                # Show trust warnings if any
                trust_indicator = "🔒" if trust_analysis['trust_score'] >= 70 else "⚠️" if trust_analysis['trust_score'] >= 50 else "🚨"
                kind = "Independent" if is_independent else "Fork"

                print(f"    📦 {fork_full_name} [{kind}] {trust_indicator}")
                print(f"       Score: {scored['score']}, Trust: {trust_analysis['trust_score']}%, {', '.join(scored['signals'][:3])}")

                if trust_analysis['warnings']:
                    for warning in trust_analysis['warnings'][:2]:  # Show max 2 warnings
                        print(f"       ⚠️  {warning}")
            elif trust_analysis['trust_score'] < 40:
                print(f"    🚨 {fork_full_name} - REJECTED (Trust: {trust_analysis['trust_score']}%)")
                if trust_analysis['warnings']:
                    print(f"       ⚠️  {trust_analysis['warnings'][0]}")

        # Sort by score (highest first)
        fork_candidates.sort(key=lambda x: x['score'], reverse=True)

        print(f"  ℹ️  {len(fork_candidates)} candidates match criteria")
        
        return fork_candidates[:5]  # Return top 5
    
    def analyze_contributor_trust(self, full_repo_name: str) -> Dict:
        """
        Analyze contributors to detect suspicious patterns.
        Returns trust metrics and warning signals.
        Optimized: Only checks top 5 contributors for speed.
        """
        try:
            url = f'https://api.github.com/repos/{full_repo_name}/contributors'
            params = {'per_page': 10}  # Get top 10
            
            headers = {}
            if self.config.github_token:
                headers['Authorization'] = f'token {self.config.github_token}'
            
            response = requests.get(url, params=params, headers=headers, timeout=10)
            
            if response.status_code != 200:
                return {'trust_score': 50, 'warnings': ['Could not fetch contributors'], 'contributor_count': 0}
            
            contributors = response.json()
            
            if not contributors:
                return {'trust_score': 0, 'warnings': ['No contributors found'], 'contributor_count': 0}
            
            trust_score = 100  # Start at 100, deduct for red flags
            warnings = []
            signals = []
            
            total_contributors = len(contributors)
            
            # Analyze each contributor
            trusted_count = 0
            suspicious_count = 0
            new_accounts = 0
            
            # OPTIMIZED: Only deep check top 5 contributors for speed
            for contributor in contributors[:5]:
                login = contributor.get('login', '')
                
                # Get detailed user info
                user_data = self.get_user_details(login)
                
                if user_data:
                    # CHECK 1: Account age
                    created_at = user_data.get('created_at')
                    if created_at:
                        account_age_days = (datetime.now() - datetime.strptime(created_at, '%Y-%m-%dT%H:%M:%SZ')).days
                        
                        if account_age_days < 90:  # Account < 3 months old
                            new_accounts += 1
                            if account_age_days < 30:
                                trust_score -= 10
                                suspicious_count += 1
                    
                    # CHECK 2: Public repos (legit users usually have multiple projects)
                    public_repos = user_data.get('public_repos', 0)
                    if public_repos == 0 or public_repos == 1:
                        trust_score -= 5
                        suspicious_count += 1
                    elif public_repos > 5:
                        trusted_count += 1
                    
                    # CHECK 3: Followers (legit devs usually have some followers)
                    followers = user_data.get('followers', 0)
                    if followers > 10:
                        trusted_count += 1
                    elif followers == 0:
                        suspicious_count += 1
                    
                    # CHECK 4: Bio/Name presence (bots often have empty profiles)
                    has_bio = bool(user_data.get('bio'))
                    has_name = bool(user_data.get('name'))
                    if not has_bio and not has_name:
                        trust_score -= 3
                
                # Small delay to avoid rate limit
                time.sleep(0.15)  # Reduced from 0.2
            
            # PATTERN DETECTION
            
            # RED FLAG 1: Too many new accounts contributing
            if total_contributors > 3 and new_accounts >= min(5, total_contributors) * 0.5:
                trust_score -= 20
                warnings.append(f'{new_accounts}/{min(5, total_contributors)} top contributors are new (<3mo)')
            
            # RED FLAG 2: Suspicious-to-total ratio too high
            if total_contributors > 3 and suspicious_count >= min(5, total_contributors) * 0.6:
                trust_score -= 15
                warnings.append(f'{suspicious_count}/{min(5, total_contributors)} top contributors look suspicious')
            
            # GREEN FLAG: Multiple established contributors
            if trusted_count >= 2:
                trust_score = min(trust_score + 10, 100)
                signals.append(f'{trusted_count} established contributors')
            
            # Normalize trust_score
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
            
        except Exception as e:
            # Don't fail the whole process on trust analysis errors
            return {
                'trust_score': 50,  # Neutral when can't determine
                'warnings': [f'Trust analysis error'],
                'contributor_count': 0,
                'signals': []
            }
    
    def get_user_details(self, username: str) -> Optional[Dict]:
        """
        Get detailed information about a GitHub user.
        """
        try:
            url = f'https://api.github.com/users/{username}'
            
            headers = {}
            if self.config.github_token:
                headers['Authorization'] = f'token {self.config.github_token}'
            
            response = requests.get(url, headers=headers, timeout=5)
            
            if response.status_code == 200:
                return response.json()
            
            return None
        except:
            return None
    
    def get_contributor_count(self, full_repo_name: str) -> int:
        """
        Get contributor count for a repository (best effort).
        This is a bonus signal but not critical.
        """
        try:
            url = f'https://api.github.com/repos/{full_repo_name}/contributors'
            params = {'per_page': 1, 'anon': 'true'}
            
            headers = {}
            if self.config.github_token:
                headers['Authorization'] = f'token {self.config.github_token}'
            
            response = requests.get(url, params=params, headers=headers, timeout=5)
            
            # Parse Link header to get total contributors
            if 'Link' in response.headers:
                links = response.headers['Link']
                import re
                match = re.search(r'page=(\d+)>; rel="last"', links)
                if match:
                    return int(match.group(1))
            
            # If no pagination, likely <30 contributors
            return 1
        except:
            return 0
    
    def get_commit_count(self, full_repo_name: str) -> int:
        """
        Get approximate commit count for a repository by checking pagination.
        """
        try:
            url = f'https://api.github.com/repos/{full_repo_name}/commits'
            params = {'per_page': 1}
            
            headers = {}
            if self.config.github_token:
                headers['Authorization'] = f'token {self.config.github_token}'
            
            response = requests.get(url, params=params, headers=headers)
            
            # Parse Link header to get total pages (= approximate commits)
            if 'Link' in response.headers:
                links = response.headers['Link']
                # Extract last page number
                import re
                match = re.search(r'page=(\d+)>; rel="last"', links)
                if match:
                    return int(match.group(1))
            
            # Fallback: if no pagination, repo has <100 commits
            return 50  # Estimate
        except:
            return 0
    
    def score_fork_reliability(self, fork_data: Dict) -> Dict:
        """
        Score a fork based on multiple reliability signals.
        Checks version/loader in: name, description, topics.
        Higher score = more likely to be maintained and compatible.
        
        Scoring breakdown:
        - Version found (name/desc/topics): +60
        - Loader found (name/desc/topics): +40
        - Trust score (contributor analysis): up to +50
        - Recently updated (<1mo): +40, (<3mo): +20, (<6mo): +10
        - Commits: +1 per 100 (max +40)
        - Contributors: +5 per contributor (max +20)
        - Stars: +2 per star (max +20)
        - Watchers: +1 per watcher (max +10)
        - Has been forked: +5
        - Relevant topics: +10
        """
        score = 0
        signals = []
        
        name = fork_data['name'].lower()
        description = (fork_data.get('description') or '').lower()
        topics = [t.lower() for t in fork_data.get('topics', [])]
        
        target_version = self.config.mc_version.lower()
        target_loader = self.config.loader.lower()

        # Build regex patterns with word boundaries to avoid substring false positives
        # e.g. "1.21.1" should NOT match "1.21.10", "forge" should NOT match "reforged"
        ver_escaped = re.escape(target_version)
        version_pattern = re.compile(rf'(?<![.\d]){ver_escaped}(?![.\d])')
        loader_pattern = re.compile(rf'(?<!\w){re.escape(target_loader)}(?!\w)')

        # SIGNAL 1: Version match (anywhere) - CRITICAL
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

        # SIGNAL 2: Loader match (anywhere) - IMPORTANT
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
        
        # SIGNAL 2.5: SECURITY - Trust score (NEW)
        trust_analysis = fork_data.get('trust_analysis', {})
        trust_score = trust_analysis.get('trust_score', 50)
        
        # Scale trust score: 0-100 → 0-50 points
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
        
        # Add trust warnings to signals if present
        if trust_analysis.get('warnings'):
            signals.extend([f"⚠️{w[:30]}" for w in trust_analysis['warnings'][:1]])
        
        # SIGNAL 3: Recent updates - FRESHNESS
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
        
        # SIGNAL 4: Commit count - ACTIVITY
        commit_points = min(fork_data['commit_count'] // 100, 40)
        score += commit_points
        if fork_data['commit_count'] > 0:
            if fork_data['commit_count'] >= 1000:
                signals.append(f"{fork_data['commit_count']//1000}k commits")
            else:
                signals.append(f"{fork_data['commit_count']} commits")
        
        # SIGNAL 5: Contributor count - COLLABORATION
        if fork_data.get('contributor_count', 0) > 0:
            contributor_points = min(fork_data['contributor_count'] * 5, 20)
            score += contributor_points
            if fork_data['contributor_count'] > 1:
                signals.append(f"{fork_data['contributor_count']} contributors")
        
        # SIGNAL 6: Stars - POPULARITY (bonus, don't penalize 0)
        if fork_data['stars'] > 0:
            star_points = min(fork_data['stars'] * 2, 20)
            score += star_points
            signals.append(f"{fork_data['stars']}⭐")
        
        # SIGNAL 7: Watchers - INTEREST
        if fork_data.get('watchers', 0) > 0:
            watcher_points = min(fork_data['watchers'], 10)
            score += watcher_points
        
        # SIGNAL 8: Fork count - DERIVATIVES
        if fork_data.get('forks', 0) > 0:
            score += 5
            signals.append(f"{fork_data['forks']} forks")
        
        # SIGNAL 9: Relevant generic topics
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

    def filter_branches_by_version_proximity(self, branches: List[BranchCandidate]) -> List[BranchCandidate]:
        """
        Filter branches to only keep those close to target version.
        Reduces noise by skipping very old versions (e.g., 1.14.x when looking for 1.21.x).
        """
        target_parts = self.config.mc_version.split('.')
        target_major_minor = '.'.join(target_parts[:2])  # e.g., "1.21"
        
        filtered = []
        
        for branch in branches:
            # Try to extract MC version from branch name
            # Must start with "1." to avoid matching dates like "2024-01-21"
            # Examples: mc1.21.1/dev, 1.21.1/stable, mc1.14/release
            version_match = re.search(r'(?:^|[^.\d])(1\.\d+(?:\.\d+)?)(?:[^.\d]|$)', branch.name)
            
            if version_match:
                branch_version = version_match.group(1)
                branch_parts = branch_version.split('.')
                branch_major_minor = '.'.join(branch_parts[:2])
                
                # Only keep if major.minor matches
                if branch_major_minor == target_major_minor:
                    filtered.append(branch)
            else:
                # No version in name - keep it (might be 'main', 'dev', etc.)
                filtered.append(branch)
        
        if len(filtered) < len(branches):
            removed = len(branches) - len(filtered)
            print(f"  ℹ️  Filtered {removed} branches from other MC versions (keeping {target_major_minor}.x only)")
        
        return filtered

    def get_branches(self, owner: str, repo: str) -> List[BranchCandidate]:
        """
        Fetch all branches from a repository and return them as candidates.
        """
        url = f"https://api.github.com/repos/{owner}/{repo}/branches"
        branches = []
        
        try:
            response = requests.get(url, headers=self.config.github_headers, timeout=10)
            
            if response.status_code != 200:
                print(f"  ⚠️  Could not fetch branches: HTTP {response.status_code}")
                return branches
            
            branch_data = response.json()
            
            for branch in branch_data:
                # Fetch commit details for date
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
            print(f"  ⚠️  Error fetching branches: {e}")
            return branches
    
    def get_file_from_repo(self, owner: str, repo: str, branch: str, filepath: str) -> Optional[str]:
        """
        Fetch a single file from a GitHub repository without cloning.
        Uses GitHub Raw Content API for speed (no rate limit impact).
        Falls back to GitHub API if raw fails.
        
        For multi-module projects, tries common locations.
        
        Returns file content as string, or None if not found.
        """
        # Common locations for gradle.properties in multi-module projects
        common_paths = [
            filepath,  # Root level (default)
            f"common/{filepath}",  # Common module
            f"fabric/{filepath}",  # Fabric module
            f"neoforge/{filepath}",  # NeoForge module
            f"forge/{filepath}",  # Forge module
        ]
        
        for path in common_paths:
            # Method 1: Try GitHub Raw (faster, no rate limit)
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
            
            try:
                response = requests.get(raw_url, timeout=5)
                if response.status_code == 200:
                    return response.text
            except requests.RequestException:
                pass
            
            # Method 2: Fallback to GitHub API (more reliable but uses rate limit)
            api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={branch}"
            
            try:
                response = requests.get(api_url, headers=self.config.github_headers, timeout=5)
                if response.status_code == 200:
                    data = response.json()
                    # Decode base64 content
                    content = base64.b64decode(data['content']).decode('utf-8')
                    return content
            except requests.RequestException:
                pass
            except (KeyError, ValueError):
                pass
        
        return None
    
    def parse_version_range_from_metadata(self, owner: str, repo: str, branch_name: str, loader: str) -> Optional[str]:
        """
        Parse version range from mod metadata files (fabric.mod.json or mods.toml).
        Returns the Minecraft version range string if found.
        """
        # For NeoForge/Forge - try mods.toml
        if loader in ['neoforge', 'forge']:
            # Try multiple common locations
            locations = [
                'neoforge/src/main/resources/META-INF/mods.toml',
                'forge/src/main/resources/META-INF/mods.toml',
                'src/main/resources/META-INF/mods.toml',
            ]
            
            for location in locations:
                mods_toml = self.get_file_from_repo(owner, repo, branch_name, location)
                if mods_toml:
                    # Parse TOML to find minecraft versionRange
                    # Example: [[dependencies.modid]]
                    #          modId="minecraft"
                    #          versionRange="[1.21,1.22)"
                    
                    import re
                    # Look for minecraft dependency with versionRange
                    pattern = r'\[\[dependencies\.[^\]]+\]\].*?modId\s*=\s*["\']minecraft["\'].*?versionRange\s*=\s*["\']([^"\']+)["\']'
                    match = re.search(pattern, mods_toml, re.DOTALL | re.IGNORECASE)
                    if match:
                        return match.group(1)
        
        # For Fabric - try fabric.mod.json
        elif loader == 'fabric':
            locations = [
                'common/src/main/resources/fabric.mod.json',
                'fabric/src/main/resources/fabric.mod.json',
                'src/main/resources/fabric.mod.json',
            ]
            
            for location in locations:
                fabric_json = self.get_file_from_repo(owner, repo, branch_name, location)
                if fabric_json:
                    try:
                        import json
                        data = json.loads(fabric_json)
                        mc_dep = data.get('depends', {}).get('minecraft', '')
                        if mc_dep:
                            return mc_dep
                    except:
                        pass
        
        return None
    
    def is_version_in_maven_range(self, version: str, range_str: str) -> bool:
        """
        Check if a version is within a Maven-style version range.
        
        Examples:
        - [1.21,1.22) = 1.21.x (inclusive 1.21, exclusive 1.22)
        - [1.21.1] = exactly 1.21.1
        - [1.21,) = 1.21 and above
        """
        try:
            # Parse the range
            if range_str.startswith('[') or range_str.startswith('('):
                # Remove brackets/parentheses
                range_clean = range_str.strip('[]()') 
                parts = range_clean.split(',')
                
                if len(parts) == 1:
                    # [1.21.1] - exact version
                    return version == parts[0].strip()
                
                elif len(parts) == 2:
                    min_ver = parts[0].strip() if parts[0].strip() else None
                    max_ver = parts[1].strip() if parts[1].strip() else None
                    
                    # Check minimum (inclusive with [, exclusive with ()
                    if min_ver:
                        is_inclusive = range_str.startswith('[')
                        if is_inclusive:
                            if not self._compare_versions(version, min_ver) >= 0:
                                return False
                        else:
                            if not self._compare_versions(version, min_ver) > 0:
                                return False
                    
                    # Check maximum (exclusive with ), inclusive with ])
                    if max_ver:
                        is_inclusive = range_str.endswith(']')
                        if is_inclusive:
                            if not self._compare_versions(version, max_ver) <= 0:
                                return False
                        else:
                            if not self._compare_versions(version, max_ver) < 0:
                                return False
                    
                    return True
        except:
            pass
        
        return False
    
    def is_version_in_fabric_range(self, version: str, range_str: str) -> bool:
        """
        Check if a version is within a Fabric-style version range.
        
        Examples:
        - ~1.21.0 = 1.21.x
        - >=1.21.0 = 1.21.0 and above
        - 1.21.1 = exactly 1.21.1
        """
        if not range_str:
            return False
        
        try:
            if range_str.startswith('~'):
                # ~1.21.0 means 1.21.x
                base = range_str[1:].strip()
                base_parts = base.split('.')[:2]  # Get major.minor
                version_parts = version.split('.')[:2]
                return base_parts == version_parts
            
            elif range_str.startswith('>='):
                min_ver = range_str[2:].strip()
                return self._compare_versions(version, min_ver) >= 0
            
            elif range_str.startswith('>'):
                min_ver = range_str[1:].strip()
                return self._compare_versions(version, min_ver) > 0
            
            elif range_str.startswith('<='):
                max_ver = range_str[2:].strip()
                return self._compare_versions(version, max_ver) <= 0
            
            elif range_str.startswith('<'):
                max_ver = range_str[1:].strip()
                return self._compare_versions(version, max_ver) < 0
            
            else:
                # Exact version
                return version == range_str
        except:
            pass
        
        return False
    
    def _compare_versions(self, v1: str, v2: str) -> int:
        """
        Compare two version strings.
        Returns: -1 if v1 < v2, 0 if v1 == v2, 1 if v1 > v2
        """
        try:
            parts1 = [int(x) for x in v1.split('.')]
            parts2 = [int(x) for x in v2.split('.')]
            
            # Pad to same length
            max_len = max(len(parts1), len(parts2))
            parts1 += [0] * (max_len - len(parts1))
            parts2 += [0] * (max_len - len(parts2))
            
            for p1, p2 in zip(parts1, parts2):
                if p1 < p2:
                    return -1
                elif p1 > p2:
                    return 1
            
            return 0
        except:
            # Fallback to string comparison
            if v1 < v2:
                return -1
            elif v1 > v2:
                return 1
            return 0
    
    def pre_validate_branch(self, owner: str, repo: str, branch: BranchCandidate,
                            override_loader: Optional[str] = None) -> bool:
        """
        Pre-validate a branch by downloading only gradle.properties (without cloning).
        Also checks libs.versions.toml for modern multi-module projects.
        ENHANCED: Reads metadata files (fabric.mod.json/mods.toml) to validate version ranges.
        Updates branch object with validation results.

        Args:
            override_loader: If set, validate against this loader instead of config.loader.
                             Used by cross-loader fallback to find Fabric branches.

        Returns True if compatible, False otherwise.
        """
        target_loader = override_loader or self.config.loader

        # STEP 1: Extract MC version and loader from gradle.properties
        gradle_content = self.get_file_from_repo(owner, repo, branch.name, 'gradle.properties')

        if gradle_content:
            mc_match = re.search(r'minecraft_version\s*=\s*["\']?([0-9.]+)["\']?', gradle_content)
            if not mc_match:
                mc_match = re.search(r'mc_version\s*=\s*["\']?([0-9.]+)["\']?', gradle_content)

            if mc_match:
                branch.minecraft_version = mc_match.group(1)

            # Extract loader version - try to detect target loader first
            if target_loader == 'neoforge':
                loader_match = re.search(r'neo(?:forge)?_version\s*=\s*["\']?([0-9.]+)["\']?', gradle_content)
                if loader_match:
                    branch.loader = 'neoforge'
                    branch.loader_version = loader_match.group(1)
            elif target_loader == 'forge':
                loader_match = re.search(r'forge_version\s*=\s*["\']?([0-9.]+)["\']?', gradle_content)
                if loader_match:
                    branch.loader = 'forge'
                    branch.loader_version = loader_match.group(1)
            elif target_loader == 'fabric':
                loader_match = re.search(r'fabric_(?:loader|api)_version\s*=\s*["\']?([0-9.]+)["\']?', gradle_content)
                if loader_match:
                    branch.loader = 'fabric'
                    branch.loader_version = loader_match.group(1)

            # If target loader not found, detect what loader IS present
            if not branch.loader:
                if re.search(r'fabric_(?:loader|api)_version\s*=', gradle_content):
                    branch.loader = 'fabric'
                elif re.search(r'neo(?:forge)?_version\s*=', gradle_content):
                    branch.loader = 'neoforge'
                elif re.search(r'forge_version\s*=', gradle_content):
                    branch.loader = 'forge'

            # Early reject if detected loader doesn't match target
            if branch.loader and branch.loader != target_loader:
                branch.validation_error = f"Wrong loader: found {branch.loader}, need {target_loader}"
                return False

        # STEP 1b: If MC version not found yet, try libs.versions.toml (version catalogs)
        if not branch.minecraft_version:
            libs_versions = self.get_file_from_repo(
                owner, repo, branch.name, 'gradle/libs.versions.toml')
            if libs_versions:
                try:
                    import toml as toml_parser
                    versions_data = toml_parser.loads(libs_versions)

                    if 'versions' in versions_data:
                        versions = versions_data['versions']
                        mc_version = (versions.get('minecraft')
                                      or versions.get('minecraft-version')
                                      or versions.get('game-version'))

                        if mc_version:
                            if isinstance(mc_version, dict):
                                mc_version = mc_version.get('ref') or mc_version.get('version')
                            branch.minecraft_version = str(mc_version).strip('"')

                        # Detect loader from version catalog if not already found
                        if not branch.loader:
                            if 'neoforge' in str(versions_data).lower():
                                branch.loader = 'neoforge'
                                branch.loader_version = str(
                                    versions.get('neoforge', 'unknown'))
                            elif 'fabric' in str(versions_data).lower():
                                branch.loader = 'fabric'
                except Exception:
                    pass

        # STEP 1c: If still no version/loader, try fabric.mod.json
        if not branch.minecraft_version or not branch.loader:
            fabric_json = self.get_file_from_repo(
                owner, repo, branch.name, 'src/main/resources/fabric.mod.json')
            if fabric_json:
                try:
                    import json
                    data = json.loads(fabric_json)

                    depends = data.get('depends', {})
                    mc_dep = depends.get('minecraft', '')

                    if mc_dep:
                        mc_version_match = re.search(r'(\d+\.\d+(?:\.\d+)?)', mc_dep)
                        if mc_version_match and not branch.minecraft_version:
                            branch.minecraft_version = mc_version_match.group(1)

                        if not branch.loader:
                            branch.loader = 'fabric'
                            loader_ver = depends.get(
                                'fabricloader', depends.get('fabric-loader', ''))
                            if loader_ver:
                                loader_match = re.search(
                                    r'(\d+\.\d+(?:\.\d+)?)', str(loader_ver))
                                if loader_match:
                                    branch.loader_version = loader_match.group(1)
                except Exception:
                    pass
        
        # STEP 2: Read metadata files for AUTHORITATIVE version range validation
        version_range = self.parse_version_range_from_metadata(owner, repo, branch.name, target_loader)
        
        if version_range:
            # We have a version range from metadata - this is the SOURCE OF TRUTH
            branch.version_range = version_range
            
            # Validate loader compatibility first
            if branch.loader and branch.loader != target_loader:
                branch.validation_error = f"Loader mismatch: {branch.loader} != {target_loader}"
                return False

            # Check if target version is in the range
            if target_loader in ['neoforge', 'forge']:
                is_compatible = self.is_version_in_maven_range(self.config.mc_version, version_range)
            else:  # fabric
                is_compatible = self.is_version_in_fabric_range(self.config.mc_version, version_range)
            
            if is_compatible:
                branch.is_compatible = True
                branch.validation_method = 'metadata_range'
                return True
            else:
                branch.validation_error = f"MC {self.config.mc_version} not in range {version_range}"
                return False
        
        # STEP 3: Fallback to gradle.properties exact/lenient matching
        if not branch.minecraft_version:
            branch.validation_error = 'minecraft_version not found in gradle.properties or version catalog'
            return False
        
        # Validate Minecraft version compatibility
        if not self.is_version_compatible(branch.minecraft_version, self.config.mc_version):
            branch.validation_error = f"MC version mismatch: {branch.minecraft_version} != {self.config.mc_version}"
            return False
        
        # Validate loader compatibility
        if branch.loader != target_loader:
            # Give better error message for Fabric-only mods
            if branch.loader == 'fabric' and target_loader in ['neoforge', 'forge']:
                branch.validation_error = f"Fabric-only mod (no {target_loader} version)"
            elif branch.loader in ['neoforge', 'forge'] and target_loader == 'fabric':
                branch.validation_error = f"{branch.loader.capitalize()}-only mod (no Fabric version)"
            else:
                branch.validation_error = f"Loader mismatch: {branch.loader or 'unknown'} != {target_loader}"
            return False
        
        # All validations passed
        branch.is_compatible = True
        branch.validation_method = 'gradle_properties'
        return True
    
    def is_version_compatible(self, found_version: str, target_version: str) -> bool:
        """
        Determine if a Minecraft version is compatible with the target version.
        
        Strict mode: Only exact match (1.21.10 == 1.21.10)
        Lenient mode: Same major.minor (1.21.x compatible with 1.21.y)
        """
        if self.config.strict_version:
            return found_version == target_version
        
        # Lenient mode: allow same major.minor version
        try:
            target_parts = target_version.split('.')[:2]  # ["1", "21"]
            found_parts = found_version.split('.')[:2]    # ["1", "21"]
            return target_parts == found_parts
        except:
            return False
    
    def pre_validate_branches(self, owner: str, repo: str,
                             branches: List[BranchCandidate],
                             override_loader: Optional[str] = None) -> List[BranchCandidate]:
        """
        Pre-validate multiple branches using GitHub API (no cloning required).
        Uses concurrent requests for speed.
        Returns only compatible branches.

        Args:
            override_loader: If set, validate against this loader instead of config.loader.
        """
        loader_label = override_loader or self.config.loader
        print(f"  🔍 Pre-validating {len(branches)} branches via GitHub API"
              f" (loader={loader_label})...")

        from concurrent.futures import ThreadPoolExecutor, as_completed

        compatible_branches = []

        # Use ThreadPoolExecutor for parallel API calls
        max_workers = min(10, len(branches))  # Max 10 concurrent requests

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all validation tasks
            future_to_branch = {
                executor.submit(self.pre_validate_branch, owner, repo, branch,
                                override_loader): branch
                for branch in branches
            }
            
            # Collect results as they complete
            for future in as_completed(future_to_branch):
                branch = future_to_branch[future]
                try:
                    is_compatible = future.result()
                    
                    if is_compatible:
                        # Show different indicators based on validation method
                        if branch.validation_method == 'metadata_range':
                            # Validated via metadata range (most authoritative)
                            print(f"    ✅ {branch.name}: Range {branch.version_range} → covers MC {self.config.mc_version}")
                        else:
                            # Validated via gradle.properties
                            version_indicator = "✓" if branch.minecraft_version == self.config.mc_version else "~"
                            print(f"    ✅ {branch.name}: MC {branch.minecraft_version} {version_indicator} + {branch.loader}")
                        
                        compatible_branches.append(branch)
                    else:
                        print(f"    ❌ {branch.name}: {branch.validation_error}")
                
                except Exception as e:
                    print(f"    ❌ {branch.name}: Validation error - {e}")
        
        return compatible_branches
    
    def score_branch(self, branch: BranchCandidate) -> int:
        """
        Score a branch that has already been pre-validated as compatible.
        New scoring prioritizes:
        1. Version exactness
        2. Commit recency
        3. Branch type (main/dev prioritized)
        """
        score = 0
        
        # VERSION EXACTNESS (highest priority)
        if branch.minecraft_version == self.config.mc_version:
            score += 1000  # Exact version match
        else:
            score += 500   # Close version match (already validated as compatible)
        
        # COMMIT RECENCY (very important for finding active branches)
        if branch.commit_date:
            try:
                commit_time = datetime.fromisoformat(branch.commit_date.replace('Z', '+00:00'))
                now = datetime.now(timezone.utc)
                days_old = (now - commit_time).days
                
                if days_old < 30:      # Less than 1 month old
                    score += 300
                elif days_old < 90:    # Less than 3 months old
                    score += 200
                elif days_old < 180:   # Less than 6 months old
                    score += 100
                else:                  # Older than 6 months
                    score -= 100       # Penalty for old branches
            except:
                pass
        
        # BRANCH TYPE BONUS
        name_lower = branch.name.lower()
        
        # Main/master branches (usually most up-to-date in active repos)
        if name_lower in ['main', 'master']:
            score += 150
        
        # Development branches (often have latest features)
        # Use boundary match to avoid "advent", "development-archive", etc.
        if re.search(r'(?:^|[/\-_])dev(?:$|[/\-_])', name_lower):
            score += 100
        
        return score

    def analyze_fork_diff(self, original_owner: str, original_repo: str,
                          fork_owner: str, fork_repo: str,
                          branch: str) -> Tuple[bool, int, str]:
        """
        Compare a fork branch against the original repo's default branch to
        detect clean version ports (only build/version files changed).

        Uses GitHub Compare API: GET /repos/{owner}/{repo}/compare/{base}...{head}

        Returns:
            (is_clean_port, score_bonus, description)
            - is_clean_port: True if only version/build files changed
            - score_bonus: Points to add to branch score (0-200)
            - description: Human-readable summary
        """
        # Version/build files that are expected to change in a clean port
        VERSION_FILES = {
            'gradle.properties', 'build.gradle', 'build.gradle.kts',
            'settings.gradle', 'settings.gradle.kts',
            'gradle/libs.versions.toml', 'gradle/wrapper/gradle-wrapper.properties',
        }
        # Metadata files that commonly change in ports
        METADATA_FILES = {
            'src/main/resources/META-INF/mods.toml',
            'src/main/resources/META-INF/neoforge.mods.toml',
            'src/main/resources/fabric.mod.json',
            'src/main/resources/quilt.mod.json',
        }
        SAFE_FILES = VERSION_FILES | METADATA_FILES

        compare_url = (
            f"https://api.github.com/repos/{original_owner}/{original_repo}"
            f"/compare/HEAD...{fork_owner}:{branch}"
        )

        try:
            response = requests.get(
                compare_url, headers=self.config.github_headers, timeout=10
            )
            if response.status_code != 200:
                return False, 0, f"Compare API returned {response.status_code}"

            data = response.json()
            files = data.get('files', [])
            total_files = len(files)

            if total_files == 0:
                return False, 0, "No file differences found"

            # Classify changed files
            safe_changes = []
            source_changes = []
            for f in files:
                filename = f.get('filename', '')
                if filename in SAFE_FILES or any(filename.endswith(s) for s in (
                    'gradle.properties', 'build.gradle', 'build.gradle.kts',
                    'mods.toml', 'neoforge.mods.toml', 'fabric.mod.json',
                )):
                    safe_changes.append(filename)
                else:
                    source_changes.append(filename)

            safe_count = len(safe_changes)
            source_count = len(source_changes)

            if source_count == 0:
                # Pure version port -- only build/metadata files changed
                return True, 200, (
                    f"Clean port: {safe_count} build/version files changed, "
                    f"0 source files changed"
                )
            elif source_count <= 3 and safe_count > 0:
                # Mostly clean -- minor source tweaks
                return True, 100, (
                    f"Mostly clean port: {safe_count} build files + "
                    f"{source_count} source files changed"
                )
            elif source_count <= 10:
                # Moderate changes -- still reasonable
                return False, 50, (
                    f"Moderate changes: {safe_count} build files + "
                    f"{source_count} source files changed"
                )
            else:
                # Extensive changes -- could be a rewrite
                return False, 0, (
                    f"Extensive changes: {source_count} source files changed"
                )

        except requests.RequestException as e:
            return False, 0, f"Compare API error: {e}"
        except (KeyError, ValueError):
            return False, 0, "Failed to parse compare response"

    def validate_gradle_properties(self, repo_path: Path,
                                   skip_loader_validation: bool = False) -> Tuple[bool, str]:
        """
        Validate gradle.properties file for version compatibility.
        Note: This is a secondary validation after pre-validation via API.

        Args:
            skip_loader_validation: If True, skip the loader-specific checks
                (e.g. neoforge_version presence). Used for cross-loader Fabric mods.

        Returns (is_valid, reason)
        """
        gradle_props = repo_path / "gradle.properties"
        
        if not gradle_props.exists():
            return False, "gradle.properties not found"
        
        try:
            with open(gradle_props, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Check for minecraft_version
            mc_version_match = re.search(r'minecraft_version\s*=\s*["\']?([0-9.]+)["\']?', content)
            if not mc_version_match:
                # Some mods use different property names
                mc_version_match = re.search(r'mc_version\s*=\s*["\']?([0-9.]+)["\']?', content)
            
            if mc_version_match:
                found_version = mc_version_match.group(1)
                
                if self.config.strict_version:
                    # Strict mode: exact match required
                    if found_version != self.config.mc_version:
                        return False, f"minecraft_version is {found_version}, expected {self.config.mc_version} (strict mode)"
                    return True, f"✅ Exact version match: {found_version}"
                else:
                    # Lenient mode: same major.minor acceptable
                    if self.is_version_compatible(found_version, self.config.mc_version):
                        if found_version == self.config.mc_version:
                            return True, f"✅ Exact version match: {found_version}"
                        else:
                            return True, f"⚠️  Close version match: {found_version} (target: {self.config.mc_version})"
                    else:
                        return False, f"minecraft_version is {found_version}, incompatible with {self.config.mc_version}"
            
            # Check for neoforge/forge version if applicable
            if not skip_loader_validation and self.config.loader == 'neoforge':
                neo_match = re.search(r'neo(?:forge)?_version\s*=\s*["\']?([0-9.]+)["\']?', content)
                if not neo_match:
                    return False, "neoforge_version not found in gradle.properties"
            
            return True, "gradle.properties validation passed"
            
        except Exception as e:
            return False, f"Error reading gradle.properties: {e}"
    
    def validate_build_gradle(self, repo_path: Path) -> Tuple[bool, str]:
        """
        Validate build.gradle or build.gradle.kts for version compatibility.
        Returns (is_valid, reason)
        """
        build_gradle = repo_path / "build.gradle"
        build_gradle_kts = repo_path / "build.gradle.kts"
        
        gradle_file = build_gradle if build_gradle.exists() else build_gradle_kts if build_gradle_kts.exists() else None
        
        if not gradle_file:
            return True, "No build.gradle found (not critical)"
        
        try:
            with open(gradle_file, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Check for minecraft version mentions
            if self.config.mc_version in content:
                return True, "build.gradle mentions target version"
            
            # If not found, it's not necessarily invalid (could be in gradle.properties)
            return True, "build.gradle checked (version in properties)"
            
        except Exception as e:
            return False, f"Error reading build.gradle: {e}"
    
    def classify_build_failure(
        self, stderr: str, stdout: str
    ) -> Tuple[FailureType, List[str]]:
        """
        Classify a Gradle build failure by parsing stderr/stdout.
        Returns (failure_type, list_of_missing_dependency_coordinates).
        """
        combined = (stderr or "") + "\n" + (stdout or "")
        missing_deps: List[str] = []

        # Pattern: "Could not resolve <group:artifact:version>"
        dep_patterns = [
            r"Could not find\s+([\w.\-]+:[\w.\-]+:[\w.\-+]+)",
            r"Could not resolve\s+([\w.\-]+:[\w.\-]+:[\w.\-+]+)",
            r"Could not resolve all (?:files|dependencies) for configuration",
        ]

        is_dep_failure = False
        for pattern in dep_patterns:
            matches = re.findall(pattern, combined)
            if matches:
                is_dep_failure = True
                # findall returns strings for groups; the third pattern has no group
                if isinstance(matches[0], str) and ":" in matches[0]:
                    # Strip trailing dots (sentence-ending periods)
                    missing_deps.extend(m.rstrip(".") for m in matches)

        if is_dep_failure:
            # Deduplicate while preserving order
            seen = set()
            unique_deps = []
            for dep in missing_deps:
                if dep not in seen:
                    seen.add(dep)
                    unique_deps.append(dep)
            return FailureType.DEPENDENCY_RESOLUTION, unique_deps

        return FailureType.BUILD_ERROR, []

    def compile_mod(
        self, repo_path: Path,
        extra_gradle_args: Optional[List[str]] = None
    ) -> Tuple[bool, Optional[Path], str, FailureType, List[str]]:
        """
        Compile the mod using Gradle.
        Returns (success, jar_path, message, failure_type, missing_deps)
        """
        print(f"    🔨 Compiling...")

        # Determine the Gradle wrapper command
        if os.name == 'nt':  # Windows
            gradlew = repo_path / "gradlew.bat"
        else:  # Unix-like
            gradlew = repo_path / "gradlew"

        if not gradlew.exists():
            return (False, None, "Gradle wrapper not found",
                    FailureType.BUILD_ERROR, [])

        # Make gradlew executable on Unix
        if os.name != 'nt':
            os.chmod(gradlew, 0o755)

        # Quick dependency resolution check (~30s vs 10min full build)
        try:
            print(f"    🔍 Checking dependencies...")
            dep_cmd = [
                str(gradlew), "dependencies", "--configuration",
                "compileClasspath", "--no-daemon"
            ]
            if extra_gradle_args:
                dep_cmd.extend(extra_gradle_args)

            dep_result = subprocess.run(
                dep_cmd,
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=120  # 2 minutes max
            )
            if dep_result.returncode != 0:
                fail_type, missing_deps = self.classify_build_failure(
                    dep_result.stderr, dep_result.stdout
                )
                if fail_type == FailureType.DEPENDENCY_RESOLUTION:
                    print(f"    ❌ Dependency check failed: {', '.join(missing_deps[:3])}")
                    return (False, None,
                            f"Dependency check failed: {missing_deps}",
                            fail_type, missing_deps)
                # If not a dep failure, continue with full build anyway
                print(f"    ⚠️  Dep check returned error but not dep-related, continuing build...")
        except subprocess.TimeoutExpired:
            print(f"    ⚠️  Dep check timed out, continuing with full build...")
        except Exception as e:
            print(f"    ⚠️  Dep check error ({e}), continuing with full build...")

        try:
            cmd = [str(gradlew), "build", "--no-daemon"]
            if extra_gradle_args:
                cmd.extend(extra_gradle_args)

            result = subprocess.run(
                cmd,
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=600  # 10 minutes timeout
            )

            if result.returncode != 0:
                # Classify the failure
                failure_type, missing_deps = self.classify_build_failure(
                    result.stderr, result.stdout
                )

                # Show last 15 lines from both stdout and stderr for diagnosis
                stderr_tail = '\n'.join(
                    result.stderr.split('\n')[-15:]
                ) if result.stderr else ''
                stdout_tail = '\n'.join(
                    result.stdout.split('\n')[-15:]
                ) if result.stdout else ''
                error_detail = f"Gradle build failed (exit code {result.returncode}):\n"
                if stderr_tail.strip():
                    error_detail += f"--- stderr ---\n{stderr_tail}\n"
                if stdout_tail.strip():
                    error_detail += f"--- stdout ---\n{stdout_tail}"

                if missing_deps:
                    error_detail += (
                        f"\n--- missing dependencies ---\n"
                        + "\n".join(f"  {d}" for d in missing_deps)
                    )

                return False, None, error_detail, failure_type, missing_deps

            # Find the compiled JAR
            build_libs = repo_path / "build" / "libs"

            if not build_libs.exists():
                return (False, None,
                        "build/libs directory not found after compilation",
                        FailureType.BUILD_ERROR, [])

            # Find JAR files, excluding classifiers that are not the main artifact
            exclude_suffixes = (
                '-sources.jar', '-dev.jar', '-javadoc.jar',
                '-slim.jar', '-api.jar'
            )
            jar_files = [
                f for f in build_libs.glob("*.jar")
                if not any(f.name.endswith(s) for s in exclude_suffixes)
            ]

            if not jar_files:
                return (False, None, "No JAR file found in build/libs",
                        FailureType.BUILD_ERROR, [])

            # Prefer fat jars (-all, -shadow) as they bundle dependencies
            fat_jars = [
                j for j in jar_files
                if j.name.endswith(('-all.jar', '-shadow.jar'))
            ]
            if fat_jars:
                main_jar = max(fat_jars, key=lambda j: j.stat().st_size)
            else:
                main_jar = max(jar_files, key=lambda j: j.stat().st_size)

            return True, main_jar, "Compilation successful", FailureType.NONE, []

        except subprocess.TimeoutExpired:
            return (False, None, "Compilation timeout (>10 minutes)",
                    FailureType.TIMEOUT, [])
        except Exception as e:
            return (False, None, f"Compilation error: {e}",
                    FailureType.UNKNOWN, [])
    
    def validate_jar(self, jar_path: Path) -> Tuple[bool, Optional[str], Optional[str], str]:
        """
        Validate the compiled JAR file.
        Returns (is_valid, mod_name, mod_version, message)
        """
        try:
            with zipfile.ZipFile(jar_path, 'r') as jar:
                # Look for mods.toml (NeoForge/Forge)
                toml_path = None
                for name in jar.namelist():
                    if name.endswith('mods.toml'):
                        toml_path = name
                        break
                
                if toml_path:
                    # NeoForge/Forge: parse mods.toml
                    with jar.open(toml_path) as f:
                        toml_content = f.read().decode('utf-8')
                        mod_info = toml.loads(toml_content)

                    # Extract mod information
                    if 'mods' in mod_info and len(mod_info['mods']) > 0:
                        first_mod = mod_info['mods'][0]
                        mod_name = first_mod.get('modId', 'unknown')
                        mod_version = first_mod.get('version', 'unknown')
                    else:
                        mod_name = 'unknown'
                        mod_version = 'unknown'

                    # Check Minecraft version dependency using proper range parsing
                    if 'dependencies' in mod_info:
                        for mod_id, dep_info in mod_info['dependencies'].items():
                            if isinstance(dep_info, list):
                                for dep in dep_info:
                                    if dep.get('modId') == 'minecraft':
                                        version_range = dep.get('versionRange', '')
                                        if version_range and not self.is_version_in_maven_range(self.config.mc_version, version_range):
                                            return False, mod_name, mod_version, f"JAR declares incompatible MC version: {version_range}"

                else:
                    # Try Fabric: look for fabric.mod.json
                    fabric_path = None
                    for name in jar.namelist():
                        if name == 'fabric.mod.json':
                            fabric_path = name
                            break

                    if not fabric_path:
                        return False, None, None, "Neither mods.toml nor fabric.mod.json found in JAR"

                    with jar.open(fabric_path) as f:
                        fabric_data = json.loads(f.read().decode('utf-8'))

                    mod_name = fabric_data.get('id', 'unknown')
                    mod_version = fabric_data.get('version', 'unknown')

                    # Check Minecraft version dependency
                    depends = fabric_data.get('depends', {})
                    mc_range = depends.get('minecraft', '')
                    if mc_range and isinstance(mc_range, str):
                        if not self.is_version_in_fabric_range(self.config.mc_version, mc_range):
                            return False, mod_name, mod_version, f"JAR declares incompatible MC version: {mc_range}"

                # Check minimum JAR size (should be at least 10KB for a real mod)
                if jar_path.stat().st_size < 10 * 1024:
                    return False, mod_name, mod_version, "JAR file suspiciously small (<10KB)"

                return True, mod_name, mod_version, "JAR validation passed"
                
        except zipfile.BadZipFile:
            return False, None, None, "Invalid JAR file (corrupted)"
        except Exception as e:
            return False, None, None, f"JAR validation error: {e}"
    
    def is_cross_loader_available(self) -> bool:
        """
        Check if Sinytra Connector + Forgified Fabric API are available
        on Modrinth for the target MC version. Caches the result per instance.
        """
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
            print(f"  ⚠️  Cross-loader unavailable: Sinytra Connector or "
                  f"Forgified Fabric API not found for MC {mc_version}")
        else:
            print(f"  ✅ Cross-loader available for MC {mc_version}")
        return available

    def check_modrinth(self, mod_name: str) -> Optional[Dict]:
        """
        Search Modrinth for a mod matching mod_name + target loader + MC version.

        Uses the Modrinth search API with facets to filter by loader and game
        version. Returns the best match dict with keys: slug, title, version_number,
        download_url, filename, file_size. Returns None if no match found.
        """
        base_url = "https://api.modrinth.com/v2"
        headers = {"User-Agent": MODRINTH_USER_AGENT}
        loader = self.config.loader
        mc_version = self.config.mc_version

        # Modrinth uses "forge" category for old Forge, "neoforge" for NeoForge
        loader_facet = loader.lower()

        facets = (
            f'[["categories:{loader_facet}"],'
            f'["versions:{mc_version}"],'
            f'["project_type:mod"]]'
        )

        # Split CamelCase names into words for better search
        # "JustEnoughItems" -> "Just Enough Items"
        search_query = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', mod_name)

        try:
            print(f"  🔍 Checking Modrinth for '{mod_name}' "
                  f"({loader} + MC {mc_version})...")
            resp = requests.get(
                f"{base_url}/search",
                params={"query": search_query, "facets": facets, "limit": 5},
                headers=headers,
                timeout=15
            )

            if resp.status_code != 200:
                print(f"    ⚠️  Modrinth search failed: HTTP {resp.status_code}")
                return None

            data = resp.json()
            hits = data.get("hits", [])

            if not hits:
                print(f"    ℹ️  Not found on Modrinth")
                return None

            mod_lower = mod_name.lower()
            # Normalize: remove hyphens/underscores for comparison
            # "forgified-fabric-api" -> "forgifiedfabricapi"
            mod_normalized = re.sub(r'[-_]', '', mod_lower)

            # Pick best match: prefer exact slug/title match
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

            # Fallback: check if title/slug contains the mod name words
            # e.g. "JustEnoughItems" -> words ["just","enough","items"]
            #      matches "Just Enough Items (JEI)"
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
                    # Single-word mod: match if slug or title contains the word
                    # e.g. "Create" matches slug "create" or title "Create Mod"
                    if len(words) == 1 and len(words[0]) >= 4:
                        if (words[0] == slug
                                or title_lower.startswith(words[0])
                                or title_lower.startswith(mod_lower)):
                            best = hit
                            break

            if not best:
                print(f"    ℹ️  Modrinth results don't match '{mod_name}'")
                return None

            slug = best["slug"]
            title = best["title"]
            downloads = best.get("downloads", 0)
            print(f"    ✅ Found on Modrinth: {title} ({slug}) "
                  f"- {downloads:,} downloads")

            # Fetch version files for exact loader + MC version
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
                print(f"    ⚠️  No version files for {loader} + MC {mc_version}")
                return None

            versions = ver_resp.json()
            # Pick most recent release (already sorted by date)
            version_data = None
            for v in versions:
                if v.get("version_type") == "release":
                    version_data = v
                    break
            if not version_data:
                version_data = versions[0]  # Fall back to latest (beta/alpha)

            files = version_data.get("files", [])
            if not files:
                return None

            primary = next(
                (f for f in files if f.get("primary", False)),
                files[0]
            )

            result = {
                "slug": slug,
                "title": title,
                "version_number": version_data.get("version_number", "unknown"),
                "version_type": version_data.get("version_type", "release"),
                "download_url": primary["url"],
                "filename": primary["filename"],
                "file_size": primary.get("size", 0),
                "downloads": downloads,
            }

            size_mb = result["file_size"] / (1024 * 1024)
            print(f"    📦 Version: {result['version_number']} "
                  f"({result['version_type']}) - {size_mb:.1f} MB")

            return result

        except requests.exceptions.Timeout:
            print(f"    ⚠️  Modrinth search timed out")
            return None
        except Exception as e:
            print(f"    ⚠️  Modrinth search error: {e}")
            return None

    def download_modrinth_mod(self, slug: str, mc_version: str,
                             loader: str) -> Optional[Path]:
        """
        Download the latest version of a mod from Modrinth API.

        Args:
            slug: Modrinth project slug (e.g. "connector", "forgified-fabric-api")
            mc_version: Target Minecraft version
            loader: Target loader (e.g. "neoforge")

        Returns:
            Path to the downloaded JAR, or None if download failed.
        """
        base_url = "https://api.modrinth.com/v2"
        headers = {"User-Agent": MODRINTH_USER_AGENT}

        # Try exact version first, then fall back to close versions
        versions_to_try = [mc_version]
        parts = mc_version.split('.')
        if len(parts) == 3:
            # e.g. for 1.21.10, also try 1.21.1
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

                # Pick the most recent version
                version_data = versions[0]
                files = version_data.get('files', [])
                if not files:
                    continue

                # Find primary file
                primary = next(
                    (f for f in files if f.get('primary', False)),
                    files[0]
                )
                download_url = primary['url']
                filename = primary['filename']

                print(f"    📥 Downloading {slug}: {filename} "
                      f"(MC {try_version})...")
                dl_resp = requests.get(download_url, timeout=120)
                dl_resp.raise_for_status()

                # Save to output_dir
                dest = self.config.output_dir / filename
                dest.write_bytes(dl_resp.content)
                print(f"    💾 Saved: {dest}")

                # Also copy to instance mods if configured
                if self.config.mods_path:
                    instance_dest = self.config.mods_path / filename
                    instance_dest.write_bytes(dl_resp.content)
                    print(f"    💾 Installed: {instance_dest}")

                return dest

            except Exception as e:
                logger.debug("Modrinth download error for %s (MC %s): %s",
                             slug, try_version, e)
                continue

        print(f"    ⚠️  Could not download {slug} from Modrinth for "
              f"MC {mc_version}")
        print(f"       Manual download: https://modrinth.com/mod/{slug}")
        return None

    def clone_and_compile(
        self, repo_url: str, specific_branch: Optional[str] = None,
        extra_gradle_args: Optional[List[str]] = None,
        skip_modrinth: bool = False
    ) -> CompilationResult:
        """
        Clone a repository, find compatible branch using pre-validation, compile, and validate.
        """
        print(f"\n{'='*80}")
        print(f"📦 Processing: {repo_url}")
        print(f"{'='*80}")

        is_cross_loader_attempt = False
        saved_fork_candidates = []

        try:
            # Parse repository URL
            owner, repo, url_branch = self.parse_repo_url(repo_url)
            print(f"  📍 Repository: {owner}/{repo}")
            
            # If URL contained a branch, use it as specific_branch
            if url_branch:
                specific_branch = url_branch
                print(f"  🌿 Using specified branch: {specific_branch}")
            
            # Get repository info for metadata
            repo_info = self.get_repo_info(owner, repo)
            if repo_info:
                stars = repo_info.get('stargazers_count', 0)
                forks = repo_info.get('forks_count', 0)
                print(f"  ⭐ Stars: {stars} | 🍴 Forks: {forks}")

            # ── Step 0: Check Modrinth for pre-compiled JAR ──────────
            if not specific_branch and not skip_modrinth:
                modrinth_result = self.check_modrinth(repo)

                # Cross-loader fallback: try Fabric on Modrinth if NeoForge/Forge
                # not found and cross_loader is enabled
                if (not modrinth_result
                        and self.config.cross_loader
                        and self.config.loader in ("neoforge", "forge")
                        and self.is_cross_loader_available()):
                    saved_loader = self.config.loader
                    self.config.loader = "fabric"
                    print(f"  🔄 CROSS-LOADER: Checking Modrinth for Fabric version...")
                    modrinth_result = self.check_modrinth(repo)
                    self.config.loader = saved_loader
                    if modrinth_result:
                        modrinth_result["_cross_loader"] = True

                if modrinth_result:
                    print(f"\n  📥 Downloading from Modrinth (no compilation needed)...")
                    try:
                        dl_resp = requests.get(
                            modrinth_result["download_url"],
                            headers={"User-Agent": MODRINTH_USER_AGENT},
                            timeout=120
                        )
                        dl_resp.raise_for_status()

                        filename = modrinth_result["filename"]
                        dest = self.config.output_dir / filename
                        dest.write_bytes(dl_resp.content)
                        print(f"    💾 Saved: {dest}")

                        if self.config.mods_path:
                            instance_dest = self.config.mods_path / filename
                            instance_dest.write_bytes(dl_resp.content)
                            print(f"    💾 Installed: {instance_dest}")

                        is_cross = modrinth_result.get("_cross_loader", False)
                        cross_note = " [Fabric via Sinytra Connector]" if is_cross else ""
                        print(f"\n  ✅ SUCCESS: {modrinth_result['title']} "
                              f"v{modrinth_result['version_number']} "
                              f"from Modrinth [pre-compiled]{cross_note}")

                        return CompilationResult(
                            repo_url=repo_url,
                            success=True,
                            jar_path=str(dest),
                            mod_name=modrinth_result["title"],
                            mod_version=modrinth_result["version_number"],
                            compiled_mc_version=self.config.mc_version,
                            modrinth_download=True,
                            is_cross_loader=is_cross,
                        )
                    except Exception as e:
                        print(f"    ⚠️  Modrinth download failed: {e}")
                        print(f"    ℹ️  Falling back to GitHub compilation...")

            # ── Step 1+: GitHub fork search + compilation ─────────────
            # Create temporary directory for this repo
            repo_temp_dir = Path(self.temp_dir) / repo

            # Fetch all branches
            print(f"  🔍 Fetching branches...")
            all_branches = self.get_branches(owner, repo)
            
            if not all_branches:
                return CompilationResult(
                    repo_url=repo_url,
                    success=False,
                    error="Could not fetch branches from repository"
                )
            
            print(f"  📊 Found {len(all_branches)} branches")
            
            # If specific branch provided, only try that one
            if specific_branch:
                target_branch = next((b for b in all_branches if b.name == specific_branch), None)
                if not target_branch:
                    return CompilationResult(
                        repo_url=repo_url,
                        success=False,
                        error=f"Specified branch '{specific_branch}' not found"
                    )
                
                # Pre-validate the specific branch
                print(f"  🔍 Validating specified branch via GitHub API...")
                is_compatible = self.pre_validate_branch(owner, repo, target_branch)
                
                if not is_compatible:
                    return CompilationResult(
                        repo_url=repo_url,
                        success=False,
                        error=f"Branch '{specific_branch}' is not compatible: {target_branch.validation_error}"
                    )
                
                branches_to_try = [target_branch]
            else:
                # Filter branches by version proximity (reduce noise)
                all_branches = self.filter_branches_by_version_proximity(all_branches)
                
                # Pre-validate all branches using GitHub API (no cloning yet!)
                compatible_branches = self.pre_validate_branches(owner, repo, all_branches)
                
                # Check if we have EXACT version match or only close matches
                exact_matches = [b for b in compatible_branches if b.minecraft_version == self.config.mc_version]
                close_matches = [b for b in compatible_branches if b.minecraft_version != self.config.mc_version]
                
                if not compatible_branches:
                    print(f"  ⚠️  No compatible branches in original repo")
                    should_search_forks = True
                elif not exact_matches and close_matches:
                    # We have close matches but no exact - search forks for exact version
                    print(f"  ⚠️  Only found close version matches ({close_matches[0].minecraft_version}), searching for exact {self.config.mc_version}...")
                    should_search_forks = True
                else:
                    # We have exact matches - no need to search forks
                    should_search_forks = False
                
                if should_search_forks:
                    # FALLBACK: Search for community forks
                    fork_candidates = self.search_compatible_repos(owner, repo)
                    saved_fork_candidates = fork_candidates or []
                    
                    if not fork_candidates:
                        if not compatible_branches:
                            pass  # Fall through to cross-loader check below
                        # else: fall through to use close matches from original repo
                    else:
                        # Try top-scored forks
                        print(f"\n  🎯 Trying top {len(fork_candidates)} community forks...")
                        
                        found_in_fork = False
                        for fork_result in fork_candidates:
                            fork_info = fork_result['fork']
                            fork_owner = fork_info['owner']
                            fork_repo = fork_info['repo']
                            
                            print(f"\n  📦 Checking fork: {fork_info['full_name']}")
                            print(f"     Score: {fork_result['score']}, Signals: {', '.join(fork_result['signals'])}")
                            
                            # STEP 1: Get ALL branches from this fork (including main/master)
                            fork_branches = self.get_branches(fork_owner, fork_repo)
                            
                            # STEP 2: Pre-validate to find compatible branches
                            fork_compatible = self.pre_validate_branches(fork_owner, fork_repo, fork_branches)
                            
                            # STEP 3: Check for EXACT matches in fork
                            fork_exact = [b for b in fork_compatible if b.minecraft_version == self.config.mc_version]
                            
                            if fork_exact:
                                # Found EXACT version in fork!
                                print(f"  ✅ Found EXACT version {self.config.mc_version} in fork!")

                                # DIFF ANALYSIS: Compare fork vs original
                                for fb in fork_exact:
                                    is_clean, diff_bonus, diff_desc = self.analyze_fork_diff(
                                        owner, repo, fork_owner, fork_repo, fb.name
                                    )
                                    fb.score += diff_bonus
                                    if is_clean:
                                        print(f"  🔍 Diff analysis: {diff_desc}")
                                    elif diff_bonus > 0:
                                        print(f"  🔍 Diff analysis: {diff_desc}")

                                # SECURITY WARNING
                                trust_score = fork_result.get('trust_score', 50)
                                trust_analysis = fork_info.get('trust_analysis', {})
                                
                                print(f"\n  ⚠️  SECURITY NOTICE: Using community fork (not official)")
                                print(f"      Trust Score: {trust_score}% - ", end="")
                                
                                if trust_score >= 80:
                                    print("HIGH confidence (established contributors)")
                                elif trust_score >= 60:
                                    print("MEDIUM confidence (some established contributors)")
                                elif trust_score >= 40:
                                    print("LOW confidence (new/unknown contributors)")
                                else:
                                    print("CRITICAL - Multiple red flags detected")
                                
                                if trust_analysis.get('warnings'):
                                    print(f"      Warnings:")
                                    for warning in trust_analysis['warnings']:
                                        print(f"      - {warning}")
                                
                                if trust_analysis.get('signals'):
                                    print(f"      Signals: {', '.join(trust_analysis['signals'])}")
                                
                                print(f"      Review fork: {fork_info['url']}")
                                print(f"      Compiling code from: {fork_owner}")
                                
                                owner = fork_owner
                                repo = fork_repo
                                compatible_branches = fork_exact
                                found_in_fork = True
                                break
                            
                            # STEP 4: No exact match - try to validate via metadata
                            # Check if close matches might actually support target version
                            fork_close = [b for b in fork_compatible if b.minecraft_version != self.config.mc_version]
                            
                            if fork_close:
                                print(f"  ℹ️  No exact match, checking if close matches support {self.config.mc_version}...")
                                
                                # Try to read metadata from the best candidate branch
                                best_branch = fork_close[0]
                                version_range = self.parse_version_range_from_metadata(
                                    fork_owner, fork_repo, best_branch.name, self.config.loader
                                )
                                
                                if version_range:
                                    # Check if our target is in the range
                                    is_compatible = False
                                    
                                    if self.config.loader in ['neoforge', 'forge']:
                                        is_compatible = self.is_version_in_maven_range(self.config.mc_version, version_range)
                                    else:  # fabric
                                        is_compatible = self.is_version_in_fabric_range(self.config.mc_version, version_range)
                                    
                                    if is_compatible:
                                        print(f"  ✅ Fork branch '{best_branch.name}' supports range {version_range}")
                                        print(f"     → Covers target {self.config.mc_version}!")
                                        
                                        # Mark these as compatible with target version
                                        for b in fork_close:
                                            b.version_range = version_range
                                            b.validation_method = 'fork_metadata_range'
                                        
                                        owner = fork_owner
                                        repo = fork_repo
                                        compatible_branches = fork_close
                                        found_in_fork = True
                                        print(f"  ✅ Using fork with validated version range support!")
                                        break
                                    else:
                                        print(f"  ❌ Range {version_range} does not cover {self.config.mc_version}")
                                else:
                                    print(f"  ⚠️  Could not determine version range from metadata")
                            
                            # STEP 5: Fallback - if original had nothing and fork has compatible branches
                            if fork_compatible and not close_matches:
                                owner = fork_owner
                                repo = fork_repo
                                compatible_branches = fork_compatible
                                found_in_fork = True
                                print(f"  ✅ Found {len(compatible_branches)} compatible branches in fork!")
                                break
                        
                        # If we didn't find anything better in forks, use close matches from original
                        if not found_in_fork and close_matches:
                            print(f"\n  ℹ️  No exact version in forks, using close matches from original repo")
                            compatible_branches = close_matches
                
                if not compatible_branches:
                    # CROSS-LOADER FALLBACK: Try Fabric branches via Sinytra Connector
                    if (self.config.cross_loader
                            and self.config.loader in ("neoforge", "forge")
                            and self.is_cross_loader_available()):
                        print(f"\n  🔄 CROSS-LOADER: No NeoForge branches found, "
                              f"trying Fabric fallback via Sinytra Connector...")

                        # Re-fetch branches with clean state for Fabric validation
                        fabric_all = self.get_branches(owner, repo)
                        fabric_branches = self.pre_validate_branches(
                            owner, repo, fabric_all, override_loader="fabric"
                        )

                        # If no Fabric in original repo, try forks
                        if not fabric_branches and saved_fork_candidates:
                            for fork_result in saved_fork_candidates:
                                fi = fork_result['fork']
                                print(f"  🔄 Checking fork {fi['full_name']} "
                                      f"for Fabric branches...")
                                fb = self.get_branches(fi['owner'], fi['repo'])
                                fabric_branches = self.pre_validate_branches(
                                    fi['owner'], fi['repo'], fb,
                                    override_loader="fabric"
                                )
                                if fabric_branches:
                                    owner = fi['owner']
                                    repo = fi['repo']
                                    break

                        if fabric_branches:
                            print(f"  ✅ Found {len(fabric_branches)} Fabric "
                                  f"branches for cross-loader compilation")
                            compatible_branches = fabric_branches
                            is_cross_loader_attempt = True
                        else:
                            return CompilationResult(
                                repo_url=repo_url,
                                success=False,
                                error=(f"No compatible branches in original repo "
                                       f"or forks for MC {self.config.mc_version}"
                                       f" + {self.config.loader} (also tried "
                                       f"Fabric cross-loader fallback)")
                            )
                    else:
                        return CompilationResult(
                            repo_url=repo_url,
                            success=False,
                            error=(f"No compatible branches in original repo "
                                   f"or forks for MC {self.config.mc_version}"
                                   f" + {self.config.loader}")
                        )
                
                print(f"  🎯 Found {len(compatible_branches)} compatible branches")
                
                # Score and sort compatible branches
                for branch in compatible_branches:
                    branch.score = self.score_branch(branch)
                
                compatible_branches.sort(key=lambda b: b.score, reverse=True)
                
                # Show top candidates
                print(f"\n  📋 Top candidates:")
                for i, branch in enumerate(compatible_branches[:min(3, len(compatible_branches))], 1):
                    exact_indicator = "✓" if branch.minecraft_version == self.config.mc_version else "~"
                    days_old = ""
                    if branch.commit_date:
                        try:
                            commit_time = datetime.fromisoformat(branch.commit_date.replace('Z', '+00:00'))
                            now = datetime.now(timezone.utc)
                            days = (now - commit_time).days
                            if days < 30:
                                days_old = f", {days}d ago"
                            elif days < 365:
                                days_old = f", {days//30}mo ago"
                        except:
                            pass
                    print(f"    {i}. {branch.name} (MC {branch.minecraft_version} {exact_indicator}, score: {branch.score}{days_old})")
                
                branches_to_try = compatible_branches
            
            # Try each branch until one works (now we only try pre-validated ones)
            branch_errors = []
            last_fail_type = FailureType.UNKNOWN
            last_missing_deps: List[str] = []
            last_fail_clone_dir: Optional[Path] = None
            for i, branch in enumerate(branches_to_try, 1):
                print(f"\n  🌿 Attempting [{i}/{len(branches_to_try)}]: {branch.name}")
                version_match = "exact" if branch.minecraft_version == self.config.mc_version else "close"
                print(f"     MC: {branch.minecraft_version} ({version_match}), Loader: {branch.loader} {branch.loader_version}")

                # Clean up previous attempt
                if repo_temp_dir.exists():
                    shutil.rmtree(repo_temp_dir)

                # Clone the repository with specific branch
                clone_url = f"https://github.com/{owner}/{repo}.git"
                print(f"    📥 Cloning...")

                try:
                    result = subprocess.run(
                        ["git", "clone", "-b", branch.name, "--depth", "1", clone_url, str(repo_temp_dir)],
                        capture_output=True,
                        text=True,
                        timeout=300  # 5 minutes
                    )

                    if result.returncode != 0:
                        err = f"Clone failed: {result.stderr.strip()[:200]}"
                        print(f"    ❌ {err}")
                        branch_errors.append(f"{branch.name}: {err}")
                        last_fail_type = FailureType.CLONE_ERROR
                        continue

                except subprocess.TimeoutExpired:
                    branch_errors.append(f"{branch.name}: Clone timeout")
                    print(f"    ❌ Clone timeout")
                    last_fail_type = FailureType.CLONE_ERROR
                    continue
                except Exception as e:
                    branch_errors.append(f"{branch.name}: Clone error: {e}")
                    print(f"    ❌ Clone error: {e}")
                    last_fail_type = FailureType.CLONE_ERROR
                    continue

                # Secondary validation of gradle.properties (should pass since we pre-validated)
                print(f"    🔍 Validating gradle.properties...")
                is_valid, message = self.validate_gradle_properties(
                    repo_temp_dir,
                    skip_loader_validation=is_cross_loader_attempt
                )
                if not is_valid:
                    print(f"    ❌ {message}")
                    branch_errors.append(f"{branch.name}: {message}")
                    last_fail_type = FailureType.VALIDATION_ERROR
                    continue
                print(f"    {message}")

                # Validate build.gradle
                print(f"    🔍 Validating build.gradle...")
                is_valid, message = self.validate_build_gradle(repo_temp_dir)
                if not is_valid:
                    print(f"    ❌ {message}")
                    branch_errors.append(f"{branch.name}: {message}")
                    last_fail_type = FailureType.VALIDATION_ERROR
                    continue
                print(f"    ✅ {message}")

                # Compile
                success, jar_path, message, fail_type, missing_deps = \
                    self.compile_mod(repo_temp_dir, extra_gradle_args)
                if not success:
                    print(f"    ❌ {message}")
                    branch_errors.append(f"{branch.name}: {message[:200]}")
                    # Track last failure info for the result
                    last_fail_type = fail_type
                    last_missing_deps = missing_deps
                    last_fail_clone_dir = repo_temp_dir
                    continue
                print(f"    ✅ {message}")

                # Validate JAR
                print(f"    🔍 Validating JAR...")
                is_valid, mod_name, mod_version, message = self.validate_jar(jar_path)
                if not is_valid:
                    print(f"    ❌ {message}")
                    branch_errors.append(f"{branch.name}: JAR validation: {message}")
                    continue
                print(f"    ✅ {message}")
                print(f"    📋 Mod: {mod_name} v{mod_version}")

                # Copy JAR to output directory
                dest_path = self.config.output_dir / jar_path.name
                shutil.copy2(jar_path, dest_path)
                print(f"    💾 Saved to: {dest_path}")

                # Also copy to instance mods folder if configured
                if self.config.mods_path:
                    instance_dest = self.config.mods_path / jar_path.name
                    shutil.copy2(jar_path, instance_dest)
                    print(f"    💾 Installed to: {instance_dest}")

                # Success!
                version_note = ""
                if branch.minecraft_version != self.config.mc_version:
                    version_note = f" (compiled for MC {branch.minecraft_version})"
                cross_note = ""
                if is_cross_loader_attempt:
                    cross_note = " [Fabric via Sinytra Connector]"

                print(f"\n  ✅ SUCCESS: {mod_name} v{mod_version} from branch '{branch.name}'{version_note}{cross_note}")

                return CompilationResult(
                    repo_url=repo_url,
                    success=True,
                    branch=branch.name,
                    jar_path=str(dest_path),
                    mod_name=mod_name,
                    mod_version=mod_version,
                    compiled_mc_version=branch.minecraft_version,
                    clone_dir=repo_temp_dir,
                    is_cross_loader=is_cross_loader_attempt
                )

            # All branches failed - show per-branch error detail
            error_detail = f"All {len(branches_to_try)} branches failed:\n"
            for err in branch_errors:
                error_detail += f"  - {err}\n"
            return CompilationResult(
                repo_url=repo_url,
                success=False,
                error=error_detail.strip(),
                failure_type=last_fail_type,
                missing_dependencies=last_missing_deps,
                clone_dir=last_fail_clone_dir
            )
            
        except Exception as e:
            return CompilationResult(
                repo_url=repo_url,
                success=False,
                error=f"Unexpected error: {e}"
            )
    
    def create_maven_local_init_script(self) -> Path:
        """
        Create a Gradle init script that injects mavenLocal() into all projects.
        Returns the path to the init script file.
        """
        init_script = Path(self.temp_dir) / "maven-local-init.gradle"
        init_script.write_text(
            "allprojects {\n"
            "    repositories {\n"
            "        mavenLocal()\n"
            "    }\n"
            "}\n",
            encoding="utf-8"
        )
        return init_script

    def publish_to_maven_local(
        self, repo_path: Path,
        extra_gradle_args: Optional[List[str]] = None
    ) -> bool:
        """
        Run publishToMavenLocal on a successfully compiled repo.
        Best-effort: returns False if the task doesn't exist or fails.
        """
        if os.name == 'nt':
            gradlew = repo_path / "gradlew.bat"
        else:
            gradlew = repo_path / "gradlew"

        if not gradlew.exists():
            return False

        if os.name != 'nt':
            os.chmod(gradlew, 0o755)

        cmd = [str(gradlew), "publishToMavenLocal", "--no-daemon"]
        if extra_gradle_args:
            cmd.extend(extra_gradle_args)

        try:
            result = subprocess.run(
                cmd,
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=600
            )
            if result.returncode == 0:
                print(f"    📤 Published to Maven Local (~/.m2/repository/)")
                return True
            else:
                logger.debug(
                    "publishToMavenLocal failed (task may not exist): %s",
                    result.stderr[:200] if result.stderr else ""
                )
                return False
        except (subprocess.TimeoutExpired, Exception) as e:
            logger.debug("publishToMavenLocal error: %s", e)
            return False

    def process_repos(self, repo_urls: List[str]):
        """
        Process a list of repository URLs with dependency-aware multi-pass.

        Pass 1: Compile all repos. After each success, publishToMavenLocal
                so later/retry builds can find the artifact.
        Pass 2: Retry repos that failed with DEPENDENCY_RESOLUTION, using
                a Gradle init script that injects mavenLocal().
        """
        self.temp_dir = tempfile.mkdtemp(prefix="mod_compiler_")
        print(f"🗂️  Using temporary directory: {self.temp_dir}")

        try:
            # === Pass 1 ===
            print(f"\n{'='*80}")
            print(f"📋 PASS 1: Compiling {len(repo_urls)} repositories")
            print(f"{'='*80}")

            pass1_results: Dict[str, CompilationResult] = {}
            for repo_url in repo_urls:
                try:
                    result = self.clone_and_compile(repo_url)
                except Exception as e:
                    logger.error(f"Unhandled error processing {repo_url}: {e}")
                    result = CompilationResult(
                        repo_url=repo_url,
                        success=False,
                        error=f"Unhandled error: {e}"
                    )
                pass1_results[repo_url] = result

                # After success, publish to maven local for other mods
                if result.success and result.clone_dir and result.clone_dir.exists():
                    self.publish_to_maven_local(result.clone_dir)

                time.sleep(1)

            # === Identify dependency failures for Pass 2 ===
            dep_failures = [
                url for url, r in pass1_results.items()
                if not r.success
                and r.failure_type == FailureType.DEPENDENCY_RESOLUTION
            ]

            # === Recompile Modrinth-only mods for mavenLocal ===
            # If any mod failed with DEPENDENCY_RESOLUTION, mods that were
            # downloaded from Modrinth (not compiled) won't be in mavenLocal.
            # Recompile those from GitHub so downstream mods can find them.
            if dep_failures:
                modrinth_only = [
                    url for url, r in pass1_results.items()
                    if r.success and r.modrinth_download
                ]
                if modrinth_only:
                    print(f"\n{'='*80}")
                    print(f"📤 MAVEN PUBLISH: Compiling {len(modrinth_only)} "
                          f"Modrinth-downloaded mods for mavenLocal")
                    print(f"{'='*80}")

                    for repo_url in modrinth_only:
                        print(f"\n  📤 Compiling for mavenLocal: {repo_url}")
                        try:
                            compile_result = self.clone_and_compile(
                                repo_url, skip_modrinth=True
                            )
                            if (compile_result.success
                                    and compile_result.clone_dir
                                    and compile_result.clone_dir.exists()):
                                self.publish_to_maven_local(
                                    compile_result.clone_dir
                                )
                        except Exception as e:
                            print(f"    ⚠️  Maven publish failed: {e}")
                            # Non-fatal: the Modrinth JAR is still the output

                        time.sleep(1)

            if dep_failures:
                # === Pass 2 ===
                print(f"\n{'='*80}")
                print(f"🔄 PASS 2: Retrying {len(dep_failures)} repos with "
                      f"dependency failures (mavenLocal injection)")
                print(f"{'='*80}")

                init_script = self.create_maven_local_init_script()
                maven_args = ["--init-script", str(init_script)]

                for repo_url in dep_failures:
                    prev = pass1_results[repo_url]
                    print(f"\n  🔄 Retrying: {repo_url}")
                    if prev.missing_dependencies:
                        print(f"     Previously missing: "
                              f"{', '.join(prev.missing_dependencies)}")

                    try:
                        result = self.clone_and_compile(
                            repo_url, extra_gradle_args=maven_args
                        )
                    except Exception as e:
                        logger.error(
                            f"Unhandled error retrying {repo_url}: {e}"
                        )
                        result = CompilationResult(
                            repo_url=repo_url,
                            success=False,
                            error=f"Unhandled error (pass 2): {e}"
                        )

                    # Update result
                    pass1_results[repo_url] = result

                    if result.success and result.clone_dir \
                            and result.clone_dir.exists():
                        self.publish_to_maven_local(
                            result.clone_dir, maven_args
                        )

                    time.sleep(1)

            # Collect final results
            self.results = list(pass1_results.values())

            # Auto-download Sinytra Connector if any cross-loader mods were compiled
            cross_loader_mods = [
                r for r in self.results
                if r.success and r.is_cross_loader
            ]
            if cross_loader_mods:
                print(f"\n{'='*80}")
                print(f"🔄 CROSS-LOADER: {len(cross_loader_mods)} Fabric mod(s) "
                      f"need Sinytra Connector to run on NeoForge")
                print(f"{'='*80}")
                print(f"  Downloading Sinytra Connector + Forgified Fabric API...")
                self.download_modrinth_mod(
                    "connector", self.config.mc_version, "neoforge"
                )
                self.download_modrinth_mod(
                    "forgified-fabric-api", self.config.mc_version, "neoforge"
                )

        finally:
            # Cleanup
            print(f"\n🧹 Cleaning up temporary directory...")
            if os.path.exists(self.temp_dir):
                self._safe_rmtree(self.temp_dir)
    
    def _safe_rmtree(self, path: Path):
        """
        Safely remove directory tree, handling Windows permission errors with Git files.
        """
        def handle_remove_readonly(func, path, exc):
            """Error handler for Windows read-only files"""
            import stat
            if not os.access(path, os.W_OK):
                # Try to make the file writable
                os.chmod(path, stat.S_IWUSR | stat.S_IREAD)
                func(path)
            else:
                raise
        
        try:
            shutil.rmtree(path, onerror=handle_remove_readonly)
        except Exception as e:
            print(f"⚠️  Warning: Could not fully clean up {path}: {e}")
            print(f"   You may need to manually delete this directory.")
    
    def generate_report(self) -> str:
        """
        Generate a detailed report of compilation results.
        """
        report_lines = []
        report_lines.append("\n" + "="*80)
        report_lines.append("📊 COMPILATION REPORT")
        report_lines.append("="*80)
        
        successful = [r for r in self.results if r.success]
        failed = [r for r in self.results if not r.success]
        version_mismatches = [r for r in successful if r.compiled_mc_version and r.compiled_mc_version != self.config.mc_version]
        cross_loader_mods = [r for r in successful if r.is_cross_loader]

        report_lines.append(f"\n✅ Successful: {len(successful)}/{len(self.results)}")
        report_lines.append(f"❌ Failed: {len(failed)}/{len(self.results)}")
        if version_mismatches:
            report_lines.append(f"⚠️  Version warnings: {len(version_mismatches)}")
        if cross_loader_mods:
            report_lines.append(f"🔄 Cross-loader (Fabric via Connector): {len(cross_loader_mods)}")
        
        if successful:
            report_lines.append("\n" + "-"*80)
            report_lines.append("✅ SUCCESSFULLY COMPILED MODS:")
            report_lines.append("-"*80)
            
            for result in successful:
                report_lines.append(f"\n📦 {result.repo_url}")
                report_lines.append(f"   🌿 Branch: {result.branch}")
                report_lines.append(f"   📋 Mod: {result.mod_name} v{result.mod_version}")

                # Show version match status
                if result.compiled_mc_version == self.config.mc_version:
                    report_lines.append(f"   ✅ Version: {result.compiled_mc_version} (exact match)")
                else:
                    report_lines.append(f"   ⚠️  Version: {result.compiled_mc_version} (target was {self.config.mc_version})")

                if result.is_cross_loader:
                    report_lines.append(f"   🔄 Fabric mod via Sinytra Connector")

                report_lines.append(f"   💾 JAR: {result.jar_path}")
        
        if version_mismatches:
            report_lines.append("\n" + "-"*80)
            report_lines.append("⚠️  VERSION WARNINGS:")
            report_lines.append("-"*80)
            report_lines.append("Some mods were compiled for slightly different Minecraft versions.")
            report_lines.append("These will likely work, but TEST IN-GAME before using in production:")
            report_lines.append("")
            
            for result in version_mismatches:
                report_lines.append(f"  • {result.mod_name} v{result.mod_version}: Compiled for {result.compiled_mc_version} (you're using {self.config.mc_version})")
            
            report_lines.append("")
            report_lines.append("To require exact version matches, use --strict flag.")
        
        if failed:
            report_lines.append("\n" + "-"*80)
            report_lines.append("❌ FAILED COMPILATIONS:")
            report_lines.append("-"*80)
            
            for result in failed:
                report_lines.append(f"\n📦 {result.repo_url}")
                report_lines.append(f"   ❌ Error: {result.error}")
                if result.failure_type == FailureType.DEPENDENCY_RESOLUTION:
                    report_lines.append(f"   🔗 Type: Unresolved dependencies")
                    if result.missing_dependencies:
                        for dep in result.missing_dependencies:
                            report_lines.append(f"      - {dep}")
        
        if cross_loader_mods:
            report_lines.append("\n" + "-"*80)
            report_lines.append("🔄 CROSS-LOADER MODS (Fabric via Sinytra Connector):")
            report_lines.append("-"*80)
            report_lines.append("These Fabric mods were compiled because no NeoForge version was found.")
            report_lines.append("They require Sinytra Connector + Forgified Fabric API to run on NeoForge.")
            report_lines.append("Compatibility is ~85% - some mods may have issues. TEST IN-GAME.")
            report_lines.append("")
            for result in cross_loader_mods:
                report_lines.append(f"  • {result.mod_name} v{result.mod_version} ({result.repo_url})")
            report_lines.append("")
            report_lines.append("Sinytra Connector: https://modrinth.com/mod/connector")
            report_lines.append("Forgified Fabric API: https://modrinth.com/mod/forgified-fabric-api")

        report_lines.append("\n" + "="*80)
        report_lines.append(f"🎯 Target: Minecraft {self.config.mc_version} with {self.config.loader.capitalize()} {self.config.loader_version}")
        if self.config.strict_version:
            report_lines.append(f"🔒 Mode: STRICT (exact version matches only)")
        else:
            report_lines.append(f"🔓 Mode: LENIENT (allows same major.minor versions)")
        report_lines.append(f"📁 Output: {self.config.output_dir}")
        if self.config.mods_path:
            report_lines.append(f"📁 Instance Mods: {self.config.mods_path}")
        report_lines.append("="*80)
        
        return '\n'.join(report_lines)


def main():
    parser = argparse.ArgumentParser(
        description="Automatically compile Minecraft mods from GitHub repositories",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Compile to out/ directory (default)
  %(prog)s --mc-version 1.21.10 --loader neoforge --loader-version 64 repos.txt

  # Compile and install to instance
  %(prog)s --mc-version 1.21.10 --loader neoforge --loader-version 64 --instance "~/.minecraft/instances/MyInstance" repos.txt

  # Custom output directory
  %(prog)s --mc-version 1.21.10 --loader neoforge --loader-version 64 --output-dir build/mods repos.txt
        """
    )
    
    parser.add_argument(
        'repos_file',
        help='Text file containing GitHub repository URLs (one per line)'
    )
    
    parser.add_argument(
        '--mc-version',
        required=True,
        help='Minecraft version (e.g., 1.21.10)'
    )
    
    parser.add_argument(
        '--loader',
        required=True,
        choices=['forge', 'neoforge', 'fabric'],
        help='Mod loader type'
    )
    
    parser.add_argument(
        '--loader-version',
        required=True,
        help='Mod loader version (e.g., 64 for NeoForge)'
    )
    
    parser.add_argument(
        '--instance',
        help='Path to Minecraft instance directory (optional, JARs also copied here)'
    )

    parser.add_argument(
        '--output-dir',
        default='out',
        help='Directory for compiled JARs (default: out/)'
    )
    
    parser.add_argument(
        '--github-token',
        help='GitHub Personal Access Token (recommended to avoid rate limits)'
    )
    
    parser.add_argument(
        '--strict',
        action='store_true',
        help='Require exact Minecraft version match (default: lenient mode allows same major.minor versions)'
    )
    
    parser.add_argument(
        '--no-cross-loader',
        action='store_true',
        help='Disable Fabric fallback via Sinytra Connector for NeoForge'
    )

    parser.add_argument(
        '--output-report',
        help='Path to save the compilation report (optional)'
    )

    parser.add_argument(
        '--log-file',
        help='Path to write a log file (optional, in addition to stdout)'
    )

    args = parser.parse_args()

    # Setup logging
    setup_logging(args.log_file)
    
    # Read repository URLs
    repos_file = Path(args.repos_file)
    if not repos_file.exists():
        print(f"❌ Error: Repository file not found: {args.repos_file}")
        sys.exit(1)
    
    with open(repos_file, 'r', encoding='utf-8') as f:
        repo_urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]
    
    if not repo_urls:
        print(f"❌ Error: No repository URLs found in {args.repos_file}")
        sys.exit(1)
    
    print(f"📋 Loaded {len(repo_urls)} repositories from {args.repos_file}")
    
    # Create configuration
    try:
        config = ModCompilerConfig(
            mc_version=args.mc_version,
            loader=args.loader,
            loader_version=args.loader_version,
            instance_path=args.instance,
            github_token=args.github_token,
            strict_version=args.strict,
            output_dir=args.output_dir,
            cross_loader=not args.no_cross_loader
        )
    except ValueError as e:
        print(f"❌ Configuration error: {e}")
        sys.exit(1)
    
    # Create compiler and process
    compiler = ModAutoCompiler(config)
    compiler.process_repos(repo_urls)
    
    # Generate and display report
    report = compiler.generate_report()
    print(report)
    
    # Save report if requested
    if args.output_report:
        with open(args.output_report, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"\n💾 Report saved to: {args.output_report}")


if __name__ == "__main__":
    main()