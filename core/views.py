import re
from django.shortcuts import render, redirect
from django.contrib import messages
from .models import AnalyzedRepository, OnboardingProgress
from .checklist import generate_onboarding_checklist
from .github_service import (
    analyze_and_save_repository,
    search_github_repositories,
    fetch_github_metadata,
    fetch_repository_documentation,
    parse_github_url,
    InvalidURLError,
    RepositoryNotFoundError,
    NetworkError,
    GitHubServiceError,
    RateLimitError,
    AuthenticationError,
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
                if matched:
                    item["readiness_score"] = matched.readiness_score
                    item["readiness_breakdown"] = matched.get_readiness_breakdown() if matched.readiness_score is not None else None
                    item["has_issues"] = matched.has_issues
                    item["open_issues_count"] = matched.open_issues_count
                    item["issues_count_is_stale"] = matched.issues_count_is_stale
                    item["display_issues_count"] = matched.display_issues_count
                    item["issue_tracker_url"] = matched.issue_tracker_url
                else:
                    item["readiness_score"] = None
                    item["readiness_breakdown"] = None
                    item["has_issues"] = item.get("has_issues", True)
                    # For unanalysed repositories, GitHub Search Repositories API returns raw open_issues_count
                    # which includes pull requests. Explicitly flag as unanalyzed rather than presenting an unverified count.
                    item["open_issues_count"] = None
                    item["issues_count_is_stale"] = False
                    item["display_issues_count"] = "Unanalyzed"

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


def mentor(request):
    """
    OSS Mentor view: Guided repository onboarding assistant.
    Provides repository overview, verified documentation excerpts, a customized
    10-step progress checklist, and contribution preparation summary.
    """
    if not request.session.session_key:
        request.session.save()
    session_key = request.session.session_key

    selected_repo = None
    repo_data = None
    doc_data = None
    checklist = None
    progress = None
    error_message = None
    doc_warning = None

    # Available analyzed repositories for dropdown selection
    analyzed_repos = AnalyzedRepository.objects.order_by("-updated_at")[:10]

    input_repo = (
        request.POST.get("repo_input", "")
        or request.GET.get("repo_url", "")
        or request.GET.get("repo", "")
    ).strip()

    if input_repo:
        try:
            # Handle either full URL or owner/name
            if input_repo.startswith(("http://", "https://", "github.com", "www.github.com")):
                owner, name = parse_github_url(input_repo)
            elif "/" in input_repo and len(input_repo.split("/")) == 2:
                owner, name = input_repo.split("/", 1)
                owner, name = owner.strip(), name.strip()
                valid_pattern = re.compile(r"^[A-Za-z0-9_.-]+$")
                if not valid_pattern.match(owner) or not valid_pattern.match(name):
                    raise InvalidURLError("Repository owner and name must contain only alphanumeric characters, dashes, dots, or underscores.")
            else:
                raise InvalidURLError("Please provide a valid GitHub repository URL (e.g., https://github.com/owner/repository) or owner/repo format.")

            # Check if this repository was already analyzed in RepoPilot
            selected_repo = AnalyzedRepository.objects.filter(owner__iexact=owner, name__iexact=name).first()

            if selected_repo:
                repo_data = selected_repo
                fallback_standards = {
                    "has_readme": selected_repo.has_readme,
                    "has_license": selected_repo.has_license,
                    "has_contributing": selected_repo.has_contributing,
                    "has_code_of_conduct": selected_repo.has_code_of_conduct,
                    "has_security": selected_repo.has_security,
                    "issue_tracker_url": selected_repo.issue_tracker_url,
                }
                has_issues = selected_repo.has_issues
            else:
                raw_metadata = fetch_github_metadata(owner, name)
                repo_data = raw_metadata
                fallback_standards = {
                    "has_readme": raw_metadata.get("has_readme", False),
                    "has_license": raw_metadata.get("has_license", False),
                    "has_contributing": raw_metadata.get("has_contributing", False),
                    "has_code_of_conduct": raw_metadata.get("has_code_of_conduct", False),
                    "has_security": raw_metadata.get("has_security", False),
                    "issue_tracker_url": raw_metadata.get("issue_tracker_url"),
                }
                has_issues = raw_metadata.get("has_issues", True)

            # Fetch verified repository documentation with graceful fallback
            doc_data = fetch_repository_documentation(
                owner,
                name,
                fallback_standards=fallback_standards,
                has_issues=has_issues,
            )

            # If an external tracker was newly discovered, update selected_repo if present
            if selected_repo and doc_data.get("issue_tracker_url") and not selected_repo.issue_tracker_url:
                selected_repo.issue_tracker_url = doc_data["issue_tracker_url"]
                selected_repo.save(update_fields=["issue_tracker_url"])

            # Set non-fatal warning if partial documentation failed due to network timeout
            if doc_data.get("readme_error") and doc_data.get("community_error"):
                doc_warning = "Network timeout while fetching live documentation from GitHub. Displaying saved repository standards and onboarding steps."
            elif doc_data.get("readme_error"):
                doc_warning = "Network timeout while fetching README excerpt. Repository details and onboarding steps remain available."
            elif doc_data.get("community_error"):
                doc_warning = "Network timeout while verifying community guidelines. Displaying saved repository standards."

            # Get or create session onboarding progress
            progress, _ = OnboardingProgress.objects.get_or_create(
                session_key=session_key,
                repo_owner=owner,
                repo_name=name,
                defaults={"repository": selected_repo if selected_repo else None},
            )

            if selected_repo and not progress.repository:
                progress.repository = selected_repo
                progress.save(update_fields=["repository"])

            # Generate checklist with completion status
            checklist = generate_onboarding_checklist(repo_data, doc_data, progress.completed_steps)

        except (InvalidURLError, RepositoryNotFoundError, RateLimitError, AuthenticationError, NetworkError, GitHubServiceError) as e:
            error_message = str(e)
        except Exception as e:
            error_message = f"An unexpected error occurred: {str(e)}"

    context = {
        "input_repo": input_repo,
        "selected_repo": selected_repo,
        "repo_data": repo_data,
        "doc_data": doc_data,
        "checklist": checklist,
        "progress": progress,
        "analyzed_repos": analyzed_repos,
        "error_message": error_message,
        "doc_warning": doc_warning,
    }
    return render(request, "core/mentor.html", context)


def mentor_toggle_step(request):
    """
    POST-only endpoint to toggle completion status of an onboarding checklist step.
    Strictly isolated by session_key to prevent cross-session progress leakage.
    """
    if request.method != "POST":
        return redirect("mentor")

    if not request.session.session_key:
        request.session.save()
    session_key = request.session.session_key

    step_id = request.POST.get("step_id", "").strip()
    repo_owner = request.POST.get("repo_owner", "").strip()
    repo_name = request.POST.get("repo_name", "").strip()

    valid_step_ids = {f"step_{i}" for i in range(1, 11)}
    if step_id in valid_step_ids and repo_owner and repo_name:
        progress, _ = OnboardingProgress.objects.get_or_create(
            session_key=session_key,
            repo_owner=repo_owner,
            repo_name=repo_name,
        )

        steps = list(progress.completed_steps or [])
        if step_id in steps:
            steps.remove(step_id)
        else:
            steps.append(step_id)

        progress.completed_steps = steps
        progress.save(update_fields=["completed_steps", "updated_at"])

    return redirect(f"/mentor/?repo={repo_owner}/{repo_name}")
