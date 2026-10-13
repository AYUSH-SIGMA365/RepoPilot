"""
Checklist engine for RepoPilot's OSS Mentor module.
Generates a structured, repository-aware 10-step onboarding workflow
for beginners preparing to make their first open-source contribution.
"""

def _get_field(data, key, default=""):
    if isinstance(data, dict):
        return data.get(key, default)
    return getattr(data, key, default)


def generate_onboarding_checklist(repo_data: dict, doc_data: dict, completed_steps: list = None, **kwargs) -> dict:
    """
    Constructs a 10-step actionable onboarding checklist customized for the selected repository.
    Integrates verified documentation links when present and computes completion metrics.
    """
    if completed_steps is None:
        completed_steps = kwargs.get("completed_step_ids", [])
    owner = _get_field(repo_data, "owner", "owner")
    name = _get_field(repo_data, "name", "repository")
    language = _get_field(repo_data, "primary_language") or "general"
    repo_url = _get_field(repo_data, "url") or f"https://github.com/{owner}/{name}"

    readme_url = doc_data.get("readme_url") if isinstance(doc_data, dict) else None
    has_readme = bool(doc_data.get("has_readme")) if isinstance(doc_data, dict) else False

    contributing_url = doc_data.get("contributing_url") if isinstance(doc_data, dict) else None
    has_contributing = bool(doc_data.get("has_contributing")) if isinstance(doc_data, dict) else False

    coc_url = (doc_data.get("coc_url") or doc_data.get("code_of_conduct_url")) if isinstance(doc_data, dict) else None
    has_coc = bool(doc_data.get("has_code_of_conduct") or coc_url) if isinstance(doc_data, dict) else False

    has_issues = _get_field(repo_data, "has_issues", None)
    if has_issues is None or has_issues == "":
        has_issues = True
    else:
        has_issues = bool(has_issues)

    issue_tracker_url = (doc_data.get("issue_tracker_url") if isinstance(doc_data, dict) else None) or _get_field(repo_data, "issue_tracker_url", None)

    if has_issues:
        issues_url = f"https://github.com/{owner}/{name}/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22"
        step_9_guidance = f"Explore open issues on {owner}/{name} with the 'good first issue' label, or search OSS Navigator for related beginner opportunities."
        step_9_doc_link = issues_url
        step_9_doc_link_text = "Browse Good First Issues on GitHub"
        step_9_explanation = "Start by tackling an issue labeled 'good first issue', 'help wanted', or 'documentation' rather than a complex architectural refactor."
    elif issue_tracker_url:
        step_9_guidance = f"GitHub Issues are disabled for {owner}/{name}. This project tracks bugs and tasks on its official tracker ({issue_tracker_url})."
        step_9_doc_link = issue_tracker_url
        step_9_doc_link_text = "Browse Official Issue Tracker"
        step_9_explanation = "GitHub Issues are disabled for this project. Check the official issue tracker for beginner-friendly tickets or bug reports."
    elif contributing_url:
        step_9_guidance = f"GitHub Issues are disabled for {owner}/{name}. Review the CONTRIBUTING guide for instructions on finding or filing tickets."
        step_9_doc_link = contributing_url
        step_9_doc_link_text = "Review Issue Guidelines in CONTRIBUTING"
        step_9_explanation = "GitHub Issues are disabled for this project. Consult the contribution guide to find how tasks and bugs are tracked."
    else:
        step_9_guidance = f"GitHub Issues are disabled for {owner}/{name}. Check the project README and documentation for task/issue tracking."
        step_9_doc_link = repo_url
        step_9_doc_link_text = "Visit Repository on GitHub"
        step_9_explanation = "GitHub Issues are disabled for this project. Check documentation for task tracking."

    raw_steps = [
        {
            "id": "step_1",
            "number": 1,
            "title": "Understand the Project's Purpose",
            "explanation": "Before writing code, understand what problem this repository solves, who its users are, and its core architecture.",
            "guidance": f"Read the project overview and README for {owner}/{name} on GitHub to grasp its main goals and features.",
            "doc_link": readme_url or f"{repo_url}#readme",
            "doc_link_text": "View README on GitHub" if has_readme else "Visit GitHub Repository",
            "is_verified": has_readme,
        },
        {
            "id": "step_2",
            "number": 2,
            "title": "Review Contribution Guidelines",
            "explanation": "Every open-source project has rules regarding pull request etiquette, branch naming, commit formats, and code style.",
            "guidance": f"Carefully review the CONTRIBUTING guide for {owner}/{name}." if has_contributing else "No formal CONTRIBUTING.md was detected. Follow standard GitHub open source conventions and polite communication.",
            "doc_link": contributing_url,
            "doc_link_text": "View CONTRIBUTING.md" if has_contributing else None,
            "is_verified": has_contributing,
        },
        {
            "id": "step_3",
            "number": 3,
            "title": "Identify Programming Language & Tools",
            "explanation": f"This repository primarily uses {language}. Verify you have the appropriate runtime, compiler, or package manager installed.",
            "guidance": f"Confirm your local development environment supports {language}. Check the repository documentation for minimum supported version requirements.",
            "doc_link": repo_url,
            "doc_link_text": f"Explore {language} Codebase",
            "is_verified": bool(_get_field(repo_data, "primary_language")),
        },
        {
            "id": "step_4",
            "number": 4,
            "title": "Review Environment Setup Instructions",
            "explanation": "Most projects specify how to configure environment variables, virtual environments, or containerized tooling.",
            "guidance": "Check the README or setup documentation for environment variables (like .env or config files) and required system dependencies before cloning.",
            "doc_link": readme_url or repo_url,
            "doc_link_text": "Inspect Setup Documentation",
            "is_verified": has_readme,
        },
        {
            "id": "step_5",
            "number": 5,
            "title": "Fork & Clone the Repository Locally",
            "explanation": "Fork the repository to your own GitHub account, then clone your fork locally to prepare your development workspace.",
            "guidance": f"Fork {owner}/{name} on GitHub, then run: git clone https://github.com/<your-username>/{name}.git && cd {name}",
            "doc_link": repo_url,
            "doc_link_text": "Open Repository on GitHub",
            "is_verified": True,
        },
        {
            "id": "step_6",
            "number": 6,
            "title": "Install Project Dependencies",
            "explanation": "Download and install third-party packages in an isolated environment (such as a virtual environment or node_modules).",
            "guidance": f"Follow the documented installation commands for {language} provided in the project documentation. Avoid running unreviewed third-party setup scripts.",
            "doc_link": readme_url or contributing_url or repo_url,
            "doc_link_text": "Review Installation Guide",
            "is_verified": has_readme or has_contributing,
        },
        {
            "id": "step_7",
            "number": 7,
            "title": "Run the Existing Test Suite",
            "explanation": "Running tests before making any changes ensures your local environment is correctly configured and working.",
            "guidance": "Inspect the repository's test commands documented in the README or CONTRIBUTING guide, and run the test suite to confirm all tests pass on your machine.",
            "doc_link": contributing_url or readme_url or repo_url,
            "doc_link_text": "Check Testing Instructions",
            "is_verified": has_contributing or has_readme,
        },
        {
            "id": "step_8",
            "number": 8,
            "title": "Review the Code of Conduct",
            "explanation": "Open source communities maintain conduct standards to ensure welcoming, respectful, and constructive collaboration.",
            "guidance": f"Review {owner}/{name}'s Code of Conduct to understand community expectations." if has_coc else "Maintain polite, constructive, and respectful communication when interacting with maintainers and contributors.",
            "doc_link": coc_url,
            "doc_link_text": "View CODE_OF_CONDUCT.md" if has_coc else None,
            "is_verified": has_coc,
        },
        {
            "id": "step_9",
            "number": 9,
            "title": "Find a Beginner-Friendly Issue",
            "explanation": step_9_explanation,
            "guidance": step_9_guidance,
            "doc_link": step_9_doc_link,
            "doc_link_text": step_9_doc_link_text,
            "is_verified": bool(has_issues or issue_tracker_url or contributing_url),
        },
        {
            "id": "step_10",
            "number": 10,
            "title": "Prepare Branch & Submit Pull Request",
            "explanation": "Create a dedicated feature branch for your changes, write clean commits, and submit a draft or ready pull request.",
            "guidance": "Create a new branch with: git checkout -b fix-your-issue-name. Commit with clear messages, push to your fork, and open a PR against the upstream main branch.",
            "doc_link": contributing_url or repo_url,
            "doc_link_text": "Review PR Guidelines",
            "is_verified": True,
        },
    ]

    completed_set = set(completed_steps) if isinstance(completed_steps, (list, set)) else set()

    phases = {
        1: "Preparation",
        2: "Preparation",
        3: "Environment",
        4: "Environment",
        5: "Development",
        6: "Development",
        7: "Development",
        8: "Community",
        9: "Exploration",
        10: "Contribution",
    }

    steps = []
    completed_count = 0
    for s in raw_steps:
        is_done = s["id"] in completed_set
        if is_done:
            completed_count += 1
        num = s["number"]
        steps.append({
            **s,
            "phase": s.get("phase", phases.get(num, "General")),
            "step_number": num,
            "completed": is_done,
            "is_completed": is_done,
            "description": s["explanation"],
            "link_url": s["doc_link"],
            "link_text": s["doc_link_text"],
        })

    total_count = len(steps)
    progress_percent = int((completed_count / total_count) * 100) if total_count > 0 else 0

    return {
        "steps": steps,
        "completed_count": completed_count,
        "total_count": total_count,
        "remaining_count": total_count - completed_count,
        "progress_percent": progress_percent,
        "progress_percentage": progress_percent,
        "is_all_completed": completed_count == total_count,
    }
