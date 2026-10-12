import os
import json
import re
import urllib.request
import urllib.error
import urllib.parse
from django.utils.dateparse import parse_datetime
from .models import AnalyzedRepository
from .scoring import calculate_readiness_score


class GitHubServiceError(Exception):
    """Base exception for GitHub service errors."""
    pass


class InvalidURLError(GitHubServiceError):
    """Raised when the provided GitHub URL is malformed."""
    pass


class RepositoryNotFoundError(GitHubServiceError):
    """Raised when the repository does not exist on GitHub or is private."""
    pass


class RateLimitError(GitHubServiceError):
    """Raised when GitHub API rate limit is exceeded."""
    pass


class NetworkError(GitHubServiceError):
    """Raised when network connection to GitHub fails."""
    pass


def parse_github_url(url: str) -> tuple[str, str]:
    """
    Validates and extracts owner and repository name from a GitHub URL.
    Accepts formats such as:
      - https://github.com/owner/repo
      - http://github.com/owner/repo/
      - github.com/owner/repo
      - https://github.com/owner/repo.git
    Returns (owner, repo_name).
    """
    if not url or not isinstance(url, str):
        raise InvalidURLError("Please provide a valid GitHub repository URL.")

    clean_url = url.strip()
    if not clean_url.startswith(("http://", "https://")):
        clean_url = "https://" + clean_url

    parsed = urllib.parse.urlparse(clean_url)
    hostname = (parsed.hostname or "").lower()

    if hostname not in ("github.com", "www.github.com"):
        raise InvalidURLError("Invalid host. The URL must point to github.com.")

    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]

    parts = [p for p in path.split("/") if p]
    if len(parts) != 2:
        raise InvalidURLError("Invalid URL format. Expected: https://github.com/owner/repository")

    owner = parts[0]
    name = parts[1]

    # Validate characters (alphanumeric, hyphens, underscores, dots)
    valid_name_pattern = re.compile(r"^[A-Za-z0-9_.-]+$")
    if not valid_name_pattern.match(owner) or not valid_name_pattern.match(name):
        raise InvalidURLError("Repository owner and name must contain only alphanumeric characters, dashes, dots, or underscores.")

    return owner, name


def _make_github_request(url: str, return_headers: bool = False):
    """
    Executes an HTTP GET request to the GitHub API using Python standard library.
    Optionally returns (data, response.headers) when return_headers is True.
    """
    headers = {
        "User-Agent": "RepoPilot-App",
        "Accept": "application/vnd.github.v3+json",
    }

    # Use GITHUB_TOKEN if configured in environment to avoid 60 req/hr IP limit
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        token = token.strip().strip("'\"")
        if token:
            headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            data = response.read().decode("utf-8")
            parsed_json = json.loads(data)
            if return_headers:
                return parsed_json, response.headers
            return parsed_json
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        elif e.code == 403:
            raise RateLimitError("GitHub API rate limit exceeded. Please try again later or add a GITHUB_TOKEN.")
        raise GitHubServiceError(f"GitHub API returned error code {e.code}.")
    except urllib.error.URLError as e:
        raise NetworkError(f"Network error while connecting to GitHub: {e.reason}")
    except Exception as e:
        raise GitHubServiceError(f"Unexpected error: {str(e)}")


def _fetch_collection_count(url: str) -> int:
    """
    Retrieves the exact total count of items in a GitHub collection (such as /pulls or /releases)
    using the HTTP Link pagination header with per_page=1.
    This provides the true total in a single request without downloading large payloads
    or consuming multiple rate-limited API calls.
    """
    sep = "&" if "?" in url else "?"
    paged_url = f"{url}{sep}per_page=1"

    result = _make_github_request(paged_url, return_headers=True)
    if not result:
        return 0

    data, headers = result
    if not isinstance(data, list) or len(data) == 0:
        return 0

    # Look for GitHub pagination Link header: <...&page=N>; rel="last"
    link_header = headers.get("Link", "")
    if link_header:
        match = re.search(r'[?&]page=(\d+)[^>]*>;\s*rel=["\']last["\']', link_header)
        if match:
            return int(match.group(1))

    return len(data)


def fetch_github_metadata(owner: str, name: str) -> dict:
    """
    Fetches repository details, languages, community health files, pull requests, and releases.
    """
    api_base = f"https://api.github.com/repos/{owner}/{name}"

    # 1. Base repository information
    repo_data = _make_github_request(api_base)
    if repo_data is None:
        raise RepositoryNotFoundError(f"Repository '{owner}/{name}' not found on GitHub or is private.")

    # 2. Languages breakdown
    languages_data = _make_github_request(f"{api_base}/languages")
    languages = languages_data if isinstance(languages_data, dict) else {}

    # 3. Community health files
    community_data = _make_github_request(f"{api_base}/community/profile")
    has_readme = False
    has_license = bool(repo_data.get("license"))
    has_contributing = False
    has_code_of_conduct = False
    has_security = False

    if isinstance(community_data, dict) and "files" in community_data:
        files = community_data["files"]
        has_readme = bool(files.get("readme"))
        has_license = has_license or bool(files.get("license"))
        has_contributing = bool(files.get("contributing"))
        has_code_of_conduct = bool(files.get("code_of_conduct"))
        has_security = bool(files.get("security"))

    # 4. Open Pull Requests & Actual Open Issues
    # GitHub's repository 'open_issues_count' includes open PRs.
    # We fetch the exact open PR count and subtract it to get true open issues.
    raw_open_issues = repo_data.get("open_issues_count", 0)
    open_prs_count = _fetch_collection_count(f"{api_base}/pulls?state=open")
    actual_open_issues = max(0, raw_open_issues - open_prs_count)

    # 5. Parse last activity date
    last_activity_str = repo_data.get("pushed_at") or repo_data.get("updated_at")
    last_activity_at = parse_datetime(last_activity_str) if last_activity_str else None

    # 6. Releases count and Git Tags count (paginated true totals via Link header)
    # Kept strictly separate: releases_count represents GitHub Releases, tags_count represents Git Tags.
    releases_count = _fetch_collection_count(f"{api_base}/releases")
    tags_count = _fetch_collection_count(f"{api_base}/tags")

    return {
        "owner": owner,
        "name": name,
        "url": f"https://github.com/{owner}/{name}",
        "description": repo_data.get("description"),
        "primary_language": repo_data.get("language"),
        "languages": languages,
        "stars_count": repo_data.get("stargazers_count", 0),
        "forks_count": repo_data.get("forks_count", 0),
        "open_issues_count": actual_open_issues,
        "open_prs_count": open_prs_count,
        "releases_count": releases_count,
        "tags_count": tags_count,
        "last_activity_at": last_activity_at,
        "has_readme": has_readme,
        "has_license": has_license,
        "has_contributing": has_contributing,
        "has_code_of_conduct": has_code_of_conduct,
        "has_security": has_security,
    }


def analyze_and_save_repository(url: str) -> AnalyzedRepository:
    """
    Validates URL, fetches data from GitHub, calculates the readiness score,
    and saves/updates the record in PostgreSQL.
    """
    owner, name = parse_github_url(url)
    metadata = fetch_github_metadata(owner, name)
    scoring_result = calculate_readiness_score(metadata)

    defaults = {
        "url": metadata["url"],
        "description": metadata["description"],
        "primary_language": metadata["primary_language"],
        "languages": metadata["languages"],
        "stars_count": metadata["stars_count"],
        "forks_count": metadata["forks_count"],
        "open_issues_count": metadata["open_issues_count"],
        "open_prs_count": metadata["open_prs_count"],
        "releases_count": metadata["releases_count"],
        "tags_count": metadata["tags_count"],
        "last_activity_at": metadata["last_activity_at"],
        "has_readme": metadata["has_readme"],
        "has_license": metadata["has_license"],
        "has_contributing": metadata["has_contributing"],
        "has_code_of_conduct": metadata["has_code_of_conduct"],
        "has_security": metadata["has_security"],
        "readiness_score": scoring_result["total_score"],
    }

    # update_or_create ensures idempotence and respects the unique constraint on (owner, name)
    repo, created = AnalyzedRepository.objects.update_or_create(
        owner=owner,
        name=name,
        defaults=defaults,
    )
    return repo


def search_github_repositories(
    query: str = "",
    language: str = "",
    topic: str = "",
    beginner_friendly: bool = False,
    min_stars: int = 50,
    sort: str = "stars",
    order: str = "desc",
    page: int = 1,
    per_page: int = 12,
) -> dict:
    """
    Searches public GitHub repositories using the GitHub REST API Search endpoint.
    Builds structured queries combining language, topic, beginner-friendly qualifiers,
    and minimum stars.
    """
    parts = ["is:public", "archived:false"]

    clean_q = (query or "").strip()
    if clean_q:
        parts.append(clean_q)

    clean_lang = (language or "").strip()
    if clean_lang:
        parts.append(f"language:{clean_lang}")

    clean_topic = (topic or "").strip()
    if clean_topic:
        parts.append(f"topic:{clean_topic}")

    if beginner_friendly:
        parts.append("good-first-issues:>0")

    try:
        stars_val = max(0, int(min_stars))
    except (ValueError, TypeError):
        stars_val = 50

    if stars_val > 0:
        parts.append(f"stars:>={stars_val}")

    full_query = " ".join(parts)

    try:
        page_num = max(1, int(page))
    except (ValueError, TypeError):
        page_num = 1

    try:
        per_page_num = max(1, min(100, int(per_page)))
    except (ValueError, TypeError):
        per_page_num = 12

    params = {
        "q": full_query,
        "sort": sort,
        "order": order,
        "page": page_num,
        "per_page": per_page_num,
    }
    encoded_params = urllib.parse.urlencode(params)
    search_url = f"https://api.github.com/search/repositories?{encoded_params}"

    result = _make_github_request(search_url, return_headers=True)
    if not result:
        return {
            "total_count": 0,
            "items": [],
            "page": page_num,
            "per_page": per_page_num,
            "total_pages": 1,
            "has_next": False,
            "has_previous": False,
            "query_used": full_query,
        }

    data, headers = result
    total_count = data.get("total_count", 0) if isinstance(data, dict) else 0
    items = data.get("items", []) if isinstance(data, dict) else []

    # GitHub Search API caps total reachable results at 1,000 items
    capped_total = min(total_count, 1000)
    total_pages = max(1, (capped_total + per_page_num - 1) // per_page_num) if capped_total > 0 else 1

    return {
        "total_count": total_count,
        "items": items,
        "page": page_num,
        "per_page": per_page_num,
        "total_pages": total_pages,
        "has_next": page_num < total_pages,
        "has_previous": page_num > 1,
        "query_used": full_query,
    }
