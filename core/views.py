from django.shortcuts import render
from django.contrib import messages
from .models import AnalyzedRepository
from .github_service import (
    analyze_and_save_repository,
    search_github_repositories,
    GitHubServiceError,
    RateLimitError,
)


def home(request):
    analyzed_repo = None
    readiness_breakdown = None
    input_url = ""

    if request.method == "POST":
        input_url = request.POST.get("repo_url", "").strip()
        try:
            analyzed_repo = analyze_and_save_repository(input_url)
            messages.success(request, f"Repository '{analyzed_repo.owner}/{analyzed_repo.name}' analyzed and saved successfully!")
        except GitHubServiceError as e:
            messages.error(request, str(e))
        except Exception as e:
            messages.error(request, f"An unexpected error occurred: {str(e)}")

    if analyzed_repo:
        readiness_breakdown = analyzed_repo.get_readiness_breakdown()

    recent_repos = AnalyzedRepository.objects.order_by("-updated_at")[:6]

    context = {
        "analyzed_repo": analyzed_repo,
        "readiness_breakdown": readiness_breakdown,
        "recent_repos": recent_repos,
        "input_url": input_url,
    }
    return render(request, "core/home.html", context)


POPULAR_LANGUAGES = [
    "Python", "JavaScript", "TypeScript", "Go", "Rust",
    "Java", "C++", "Ruby", "PHP", "Swift"
]

POPULAR_TOPICS = [
    "web", "cli", "ai", "machine-learning", "api",
    "hacktoberfest", "education", "developer-tools"
]


def navigator(request):
    """
    OSS Navigator view: Searches public GitHub repositories with beginner-friendly
    filters and matches discovered repositories against existing RepoPilot analyses
    to display Contributor Readiness Scores.
    """
    is_initial_load = len(request.GET) == 0

    query = request.GET.get("query", "").strip()
    language = request.GET.get("language", "").strip()
    topic = request.GET.get("topic", "").strip()

    # Default to beginner-friendly and min 50 stars on initial load
    if is_initial_load:
        beginner_friendly = True
        min_stars = 50
    else:
        beginner_friendly = request.GET.get("beginner_friendly") in ("on", "true", "1")
        try:
            min_stars = max(0, int(request.GET.get("min_stars", 50)))
        except (ValueError, TypeError):
            min_stars = 50

    try:
        page = max(1, int(request.GET.get("page", 1)))
    except (ValueError, TypeError):
        page = 1

    search_results = None
    error_message = None

    try:
        search_results = search_github_repositories(
            query=query,
            language=language,
            topic=topic,
            beginner_friendly=beginner_friendly,
            min_stars=min_stars,
            page=page,
            per_page=12,
        )

        # Match results against existing AnalyzedRepository records in PostgreSQL
        if search_results.get("items"):
            owners = [
                item["owner"]["login"]
                for item in search_results["items"]
                if "owner" in item and "login" in item["owner"]
            ]
            names = [item["name"] for item in search_results["items"] if "name" in item]

            existing_repos_map = {
                f"{repo.owner}/{repo.name}".lower(): repo
                for repo in AnalyzedRepository.objects.filter(owner__in=owners, name__in=names)
            }

            for item in search_results["items"]:
                full_name = item.get("full_name", "")
                if not full_name and "owner" in item and "login" in item["owner"]:
                    full_name = f"{item['owner']['login']}/{item['name']}"
                matched = existing_repos_map.get(full_name.lower())

                item["analyzed_record"] = matched
                if matched and matched.readiness_score is not None:
                    item["readiness_score"] = matched.readiness_score
                    item["readiness_breakdown"] = matched.get_readiness_breakdown()
                else:
                    item["readiness_score"] = None
                    item["readiness_breakdown"] = None

    except RateLimitError as e:
        error_message = str(e)
    except GitHubServiceError as e:
        error_message = str(e)
    except Exception as e:
        error_message = f"An unexpected error occurred while searching: {str(e)}"

    # Build querystring without 'page' for clean pagination links
    params = request.GET.copy()
    if "page" in params:
        del params["page"]
    # Ensure current filter states are preserved in pagination links even on initial load
    if is_initial_load:
        params["beginner_friendly"] = "on"
        params["min_stars"] = str(min_stars)
    querystring_base = params.urlencode()

    context = {
        "search_results": search_results,
        "error_message": error_message,
        "query": query,
        "language": language,
        "topic": topic,
        "beginner_friendly": beginner_friendly,
        "min_stars": min_stars,
        "page": page,
        "querystring_base": querystring_base,
        "popular_languages": POPULAR_LANGUAGES,
        "popular_topics": POPULAR_TOPICS,
    }
    return render(request, "core/navigator.html", context)
