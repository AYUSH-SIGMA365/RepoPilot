import os
import json
import re
import base64
import time
import socket
import ssl
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


class AuthenticationError(GitHubServiceError):
    """Raised when GitHub API authentication fails or credentials are invalid."""
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


def _make_github_request(url: str, return_headers: bool = False, max_retries: int = 2):
    """
    Executes an HTTP GET request to the GitHub API using Python standard library.
    Implements bounded retries with exponential backoff for transient connection/SSL errors.
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

    attempt = 0
    while True:
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
            elif e.code == 401:
                raise AuthenticationError("GitHub authentication failed. Please verify your GITHUB_TOKEN.")
            elif e.code == 403:
                raise RateLimitError("GitHub API rate limit exceeded. Please try again later or add a GITHUB_TOKEN.")
            elif e.code in (502, 503, 504) and attempt < max_retries:
                attempt += 1
                time.sleep(0.3 * attempt)
                continue
            raise GitHubServiceError(f"GitHub API returned error code {e.code}.")
        except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError) as e:
            reason = getattr(e, "reason", str(e))
            if attempt < max_retries:
                attempt += 1
                time.sleep(0.3 * attempt)
                continue
            raise NetworkError(f"Network error while connecting to GitHub: {reason}")
        except Exception as e:
            if attempt < max_retries and "timeout" in str(e).lower():
                attempt += 1
                time.sleep(0.3 * attempt)
                continue
            raise GitHubServiceError(f"Unexpected error: {str(e)}")


def _extract_external_tracker_url(candidates: list[str]) -> str | None:
    """
    Safely inspects text or candidate URLs for verified official external bug/issue trackers
    (e.g., Trac, Jira, Bugzilla, Redmine, code.djangoproject.com, etc.).
    Validates candidate URLs and strips trailing punctuation.
    Never selects arbitrary URLs merely containing words like 'issue' or 'ticket'.
    Returns a verified URL or None. Never fabricates an unverified URL.
    """
    if not candidates:
        return None

    # Recognized tracker domains and subdomains
    trusted_tracker_hosts = {
        "code.djangoproject.com",
    }

    # Domains that should never be treated as external issue trackers
    excluded_hosts = {
        "github.com",
        "gitlab.com",
        "twitter.com",
        "x.com",
        "facebook.com",
        "linkedin.com",
        "youtube.com",
        "medium.com",
    }

    url_regex = re.compile(r"https?://[^\s<>\"'()]+", re.IGNORECASE)

    for text in candidates:
        if not text or not isinstance(text, str):
            continue

        urls = url_regex.findall(text)
        for raw_url in urls:
            # Strip trailing punctuation often found in markdown or prose
            url = raw_url.rstrip(".,:;!?)'\"`")
            try:
                parsed = urllib.parse.urlparse(url)
            except Exception:
                continue

            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                continue

            host = parsed.netloc.lower()
            # Strip port if present
            if ":" in host:
                host = host.split(":")[0]

            if (
                host in excluded_hosts
                or host.startswith("docs.")
                or host.endswith(".readthedocs.io")
                or host.endswith(".rtfd.io")
            ):
                continue

            path = parsed.path.lower()

            # 1. Explicitly trusted domain or recognized tracker subdomains
            is_tracker_host = (
                host in trusted_tracker_hosts
                or host.startswith("bugs.")
                or host.startswith("bugzilla.")
                or host.startswith("jira.")
                or host.startswith("trac.")
                or host.startswith("redmine.")
                or host.startswith("issues.")
                or ".bugzilla." in host
                or ".jira." in host
                or ".trac." in host
            )

            # 2. Known tracker endpoint paths on dedicated platforms
            is_tracker_path = any(
                endpoint in path
                for endpoint in (
                    "/newticket",
                    "/enter_bug",
                    "/show_bug.cgi",
                    "/jira/browse/",
                    "/trac/ticket",
                )
            )

            if is_tracker_host or is_tracker_path:
                return url

    return None


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
    has_issues = bool(repo_data.get("has_issues", True))
    open_prs_count = _fetch_collection_count(f"{api_base}/pulls?state=open")
    issue_tracker_url = None
    actual_open_issues = None

    if has_issues:
        # Authoritative primary source: GitHub Search API with is:issue is:open
        # which strictly isolates issues from pull requests and avoids metadata counter desync.
        try:
            search_data = _make_github_request(
                f"https://api.github.com/search/issues?q=repo:{owner}/{name}+is:issue+is:open&per_page=1"
            )
            if isinstance(search_data, dict) and "total_count" in search_data:
                actual_open_issues = int(search_data["total_count"])
        except Exception:
            actual_open_issues = None

        # When the Search API fails or is rate-limited, do NOT fabricate or calculate
        # an issue count by subtracting PRs from cached metadata, as this can produce
        # inaccurate results. Leave actual_open_issues as None (unavailable).
        issue_tracker_url = f"https://github.com/{owner}/{name}/issues?q=is%3Aissue+is%3Aopen"
    else:
        # GitHub Issues are disabled for this repository.
        # On repositories with disabled issues, GitHub's open_issues_count only reflects open PRs.
        # We do not subtract PRs or fabricate an issue count. The count is semantically unavailable.
        actual_open_issues = None
        homepage = repo_data.get("homepage") or ""
        desc = repo_data.get("description") or ""
        issue_tracker_url = _extract_external_tracker_url([homepage, desc])

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
        "has_issues": has_issues,
        "issues_count_is_stale": False,
        "issue_tracker_url": issue_tracker_url,
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
    Preserves last-known issue count when Search API fails on refresh and marks it stale.
    """
    owner, name = parse_github_url(url)
    metadata = fetch_github_metadata(owner, name)
    scoring_result = calculate_readiness_score(metadata)

    # Check for existing repository record to handle refresh logic
    existing_repo = AnalyzedRepository.objects.filter(owner__iexact=owner, name__iexact=name).first()

    has_issues = metadata.get("has_issues", True)
    fetched_issues = metadata.get("open_issues_count")
    issues_count_is_stale = False

    if not has_issues:
        # GitHub Issues are disabled for this repository: always None (N/A)
        final_open_issues = None
        issues_count_is_stale = False
    elif fetched_issues is not None:
        # Authoritative verified count from GitHub Search API
        final_open_issues = fetched_issues
        issues_count_is_stale = False
    elif existing_repo and existing_repo.open_issues_count is not None:
        # Search API failed during refresh: preserve last-known count and mark stale
        final_open_issues = existing_repo.open_issues_count
        issues_count_is_stale = True
    else:
        # Search API failed on initial analysis (no reliable last-known count):
        # Do not fabricate or estimate. Set to None (Unavailable).
        final_open_issues = None
        issues_count_is_stale = False

    defaults = {
        "url": metadata["url"],
        "description": metadata["description"],
        "primary_language": metadata["primary_language"],
        "languages": metadata["languages"],
        "stars_count": metadata["stars_count"],
        "forks_count": metadata["forks_count"],
        "open_issues_count": final_open_issues,
        "open_prs_count": metadata["open_prs_count"],
        "has_issues": has_issues,
        "issues_count_is_stale": issues_count_is_stale,
        "issue_tracker_url": metadata.get("issue_tracker_url"),
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


def fetch_repository_documentation(
    owner: str,
    name: str,
    fallback_standards: dict | None = None,
    has_issues: bool = True,
) -> dict:
    """
    Fetches verified repository documentation (README excerpt, CONTRIBUTING guidelines,
    LICENSE, CODE_OF_CONDUCT, SECURITY policy) from the GitHub REST API.
    Handles missing files safely and caps text content to prevent excessive payload sizes.
    Isolates documentation failures so that network errors on optional endpoints do not
    crash the entire onboarding assistant.
    """
    api_base = f"https://api.github.com/repos/{owner}/{name}"
    fallbacks = fallback_standards or {}

    # 1. Fetch README
    readme_data = None
    readme_error = None
    readme_content = None
    readme_url = None
    has_readme = bool(fallbacks.get("has_readme", False))

    try:
        readme_data = _make_github_request(f"{api_base}/readme")
        if isinstance(readme_data, dict):
            has_readme = True
            readme_url = readme_data.get("html_url") or f"https://github.com/{owner}/{name}#readme"
            raw_b64 = readme_data.get("content", "")
            if raw_b64 and readme_data.get("encoding") == "base64":
                try:
                    decoded_bytes = base64.b64decode(raw_b64)
                    decoded_text = decoded_bytes.decode("utf-8", errors="replace")
                    # Truncate to 4000 characters for a safe, performant preview
                    if len(decoded_text) > 4000:
                        readme_content = decoded_text[:4000] + "\n\n... [README truncated for preview. View full file on GitHub]"
                    else:
                        readme_content = decoded_text
                except Exception:
                    readme_content = None
            elif raw_b64 == "":
                readme_content = ""
        else:
            # 404 - file not found
            has_readme = False
            readme_content = None
    except (RateLimitError, AuthenticationError):
        raise
    except NetworkError as e:
        readme_error = str(e)
        readme_content = None
    except Exception as e:
        readme_error = f"Error fetching README: {str(e)}"
        readme_content = None

    # 2. Fetch community profile for verified documentation URLs
    community_data = None
    community_error = None
    contributing_url = None
    coc_url = None
    license_url = None
    license_name = None
    security_url = None
    has_contributing = bool(fallbacks.get("has_contributing", False))
    has_coc = bool(fallbacks.get("has_code_of_conduct", False))
    has_license = bool(fallbacks.get("has_license", False))
    has_security = bool(fallbacks.get("has_security", False))

    try:
        community_data = _make_github_request(f"{api_base}/community/profile")
        if isinstance(community_data, dict):
            files = community_data.get("files", {})
            contributing_info = files.get("contributing")
            has_contributing = bool(contributing_info)
            contributing_url = contributing_info.get("html_url") if isinstance(contributing_info, dict) else None

            coc_info = files.get("code_of_conduct")
            has_coc = bool(coc_info)
            coc_url = coc_info.get("html_url") if isinstance(coc_info, dict) else None

            license_info = files.get("license")
            has_license = bool(license_info)
            license_url = license_info.get("html_url") if isinstance(license_info, dict) else None
            license_name = license_info.get("name") if isinstance(license_info, dict) else None

            security_info = files.get("security")
            has_security = bool(security_info)
            security_url = security_info.get("html_url") if isinstance(security_info, dict) else None
    except (RateLimitError, AuthenticationError):
        raise
    except NetworkError as e:
        community_error = str(e)
    except Exception as e:
        community_error = f"Error fetching community guidelines: {str(e)}"

    # If GitHub issues are disabled and no external tracker was previously saved,
    # inspect the README text for verified issue tracker links (e.g. Trac, Jira, Bugzilla).
    issue_tracker_url = fallbacks.get("issue_tracker_url")
    if not has_issues and not issue_tracker_url and readme_content:
        issue_tracker_url = _extract_external_tracker_url([readme_content])

    # If both requests failed with network errors and no fallback data is available,
    # raise NetworkError to signal that GitHub is completely unreachable.
    if readme_error and community_error and not fallbacks:
        raise NetworkError(f"GitHub is unreachable: {readme_error}")

    return {
        "has_readme": has_readme,
        "readme_url": readme_url or f"https://github.com/{owner}/{name}#readme",
        "readme_content": readme_content,
        "readme_excerpt": readme_content or "",
        "readme_error": readme_error,
        "has_contributing": has_contributing,
        "contributing_url": contributing_url,
        "has_code_of_conduct": has_coc,
        "coc_url": coc_url,
        "code_of_conduct_url": coc_url,
        "has_license": has_license,
        "license_url": license_url,
        "license_name": license_name,
        "has_security": has_security,
        "security_url": security_url,
        "community_error": community_error,
        "issue_tracker_url": issue_tracker_url,
    }
