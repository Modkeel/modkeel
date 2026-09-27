"""Version parsing and comparison for Minecraft mod loaders."""

import re


def compare_versions(v1: str, v2: str) -> int:
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
    except Exception:
        # Fallback to string comparison
        if v1 < v2:
            return -1
        elif v1 > v2:
            return 1
        return 0


def is_version_in_maven_range(version: str, range_str: str) -> bool:
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
                        if not compare_versions(version, min_ver) >= 0:
                            return False
                    else:
                        if not compare_versions(version, min_ver) > 0:
                            return False

                # Check maximum (exclusive with ), inclusive with ])
                if max_ver:
                    is_inclusive = range_str.endswith(']')
                    if is_inclusive:
                        if not compare_versions(version, max_ver) <= 0:
                            return False
                    else:
                        if not compare_versions(version, max_ver) < 0:
                            return False

                return True
    except Exception:
        pass

    return False


def is_version_in_fabric_range(version: str, range_str: str) -> bool:
    """
    Check if a version is within a Fabric-style version range.

    Examples:
    - ~1.21.0 = 1.21.x
    - >=1.21.0 = 1.21.0 and above
    - 1.21.1 = exactly 1.21.1
    """
    if not range_str:
        return False

    range_str = re.sub(r'([<>=~^]+)\s+', r'\1', range_str.strip())
    if '||' in range_str:
        return any(is_version_in_fabric_range(version, part)
                   for part in range_str.split('||') if part.strip())
    if ' ' in range_str:
        return all(is_version_in_fabric_range(version, part) for part in range_str.split())

    try:
        if range_str.startswith('~'):
            # ~1.21.0 means 1.21.x
            base = range_str[1:].strip()
            base_parts = base.split('.')[:2]  # Get major.minor
            version_parts = version.split('.')[:2]
            return base_parts == version_parts

        elif range_str.startswith('>='):
            min_ver = range_str[2:].strip()
            return compare_versions(version, min_ver) >= 0

        elif range_str.startswith('>'):
            min_ver = range_str[1:].strip()
            return compare_versions(version, min_ver) > 0

        elif range_str.startswith('<='):
            max_ver = range_str[2:].strip()
            return compare_versions(version, max_ver) <= 0

        elif range_str.startswith('<'):
            max_ver = range_str[1:].strip()
            return compare_versions(version, max_ver) < 0

        else:
            # Exact version
            return version == range_str
    except Exception:
        pass

    return False


def is_version_compatible(found_version: str, target_version: str,
                          strict: bool = False) -> bool:
    """
    Determine if a Minecraft version is compatible with the target version.

    Strict mode: Only exact match (1.21.10 == 1.21.10)
    Lenient mode: Same major.minor (1.21.x compatible with 1.21.y)
    """
    if strict:
        return found_version == target_version

    # Lenient mode: allow same major.minor version
    try:
        target_parts = target_version.split('.')[:2]  # ["1", "21"]
        found_parts = found_version.split('.')[:2]    # ["1", "21"]
        return target_parts == found_parts
    except Exception:
        return False
