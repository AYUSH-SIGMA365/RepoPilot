from django.shortcuts import render
from django.contrib import messages
from .models import AnalyzedRepository
from .github_service import analyze_and_save_repository, GitHubServiceError


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
