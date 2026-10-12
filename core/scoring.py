from django.utils import timezone


def _get_val(data, key, default=None):
    """
    Helper to extract a value from either an AnalyzedRepository model instance
    or a dictionary of repository metadata.
    """
    if isinstance(data, dict):
        return data.get(key, default)
    return getattr(data, key, default)


def calculate_readiness_score(data, now=None) -> dict:
    """
    Calculates a deterministic Repository Readiness Score out of 100 based on eight
    objective criteria. Measures how prepared a repository is for new contributors.

    Criteria and maximum points:
      - README present: 20
      - CONTRIBUTING guide present: 20
      - License present: 15
      - Recent repository activity: 15
      - Code of Conduct present: 10
      - Primary language identified: 10
      - Repository description present: 5
      - Security policy present: 5
      Total: 100 points
    """
    if now is None:
        now = timezone.now()

    # 1. README documentation (20 pts)
    has_readme = bool(_get_val(data, "has_readme", False))
    readme_pts = 20 if has_readme else 0

    # 2. Open source license (15 pts)
    has_license = bool(_get_val(data, "has_license", False))
    license_pts = 15 if has_license else 0

    # 3. CONTRIBUTING guide (20 pts)
    has_contributing = bool(_get_val(data, "has_contributing", False))
    contributing_pts = 20 if has_contributing else 0

    # 4. Code of Conduct (10 pts)
    has_coc = bool(_get_val(data, "has_code_of_conduct", False))
    coc_pts = 10 if has_coc else 0

    # 5. Security policy (5 pts)
    has_security = bool(_get_val(data, "has_security", False))
    security_pts = 5 if has_security else 0

    # 6. Repository description (5 pts)
    description = _get_val(data, "description") or ""
    has_description = bool(description.strip())
    description_pts = 5 if has_description else 0

    # 7. Primary programming language (10 pts)
    primary_language = _get_val(data, "primary_language") or ""
    has_language = bool(primary_language.strip())
    language_pts = 10 if has_language else 0

    # 8. Recent activity (15 pts)
    last_activity = _get_val(data, "last_activity_at")
    activity_pts = 0
    activity_feedback = "Activity date missing"

    if last_activity is not None:
        if timezone.is_naive(last_activity):
            last_activity = timezone.make_aware(last_activity)
        if timezone.is_naive(now):
            now = timezone.make_aware(now)

        days_ago = (now - last_activity).days

        if days_ago <= 30:
            activity_pts = 15
            activity_feedback = "Updated within the last 30 days"
        elif 31 <= days_ago <= 90:
            activity_pts = 10
            activity_feedback = "Updated 31–90 days ago"
        elif 91 <= days_ago <= 180:
            activity_pts = 5
            activity_feedback = "Updated 91–180 days ago"
        else:  # days_ago > 180
            activity_pts = 0
            activity_feedback = "Updated more than 180 days ago"

    # Compute total score
    total_score = (
        readme_pts
        + license_pts
        + contributing_pts
        + coc_pts
        + security_pts
        + description_pts
        + language_pts
        + activity_pts
    )
    total_score = max(0, min(100, int(total_score)))

    # Determine status level for progress bar styling
    if total_score >= 80:
        level_class = "success"
        level_label = "High Readiness"
    elif total_score >= 50:
        level_class = "warning"
        level_label = "Moderate Readiness"
    else:
        level_class = "danger"
        level_label = "Low Readiness"

    criteria = [
        {
            "name": "README Documentation",
            "points": readme_pts,
            "max_points": 20,
            "passed": has_readme,
            "feedback": "Project overview and setup guide present." if has_readme else "Missing README. Crucial for explaining what the project does.",
        },
        {
            "name": "CONTRIBUTING Guide",
            "points": contributing_pts,
            "max_points": 20,
            "passed": has_contributing,
            "feedback": "Contribution instructions provided." if has_contributing else "Missing CONTRIBUTING.md. Helps new developers set up and submit code.",
        },
        {
            "name": "Open Source License",
            "points": license_pts,
            "max_points": 15,
            "passed": has_license,
            "feedback": "License defines usage and distribution terms." if has_license else "Missing LICENSE file.",
        },
        {
            "name": "Recent Repository Activity",
            "points": activity_pts,
            "max_points": 15,
            "passed": activity_pts > 0,
            "feedback": activity_feedback,
        },
        {
            "name": "Primary Programming Language",
            "points": language_pts,
            "max_points": 10,
            "passed": has_language,
            "feedback": f"Language identified ({primary_language.strip()})." if has_language else "Primary programming language not identified.",
        },
        {
            "name": "Code of Conduct",
            "points": coc_pts,
            "max_points": 10,
            "passed": has_coc,
            "feedback": "Community conduct expectations defined." if has_coc else "Missing CODE_OF_CONDUCT.md.",
        },
        {
            "name": "Repository Description",
            "points": description_pts,
            "max_points": 5,
            "passed": has_description,
            "feedback": "Short summary provided." if has_description else "Missing repository description.",
        },
        {
            "name": "Security Policy",
            "points": security_pts,
            "max_points": 5,
            "passed": has_security,
            "feedback": "Vulnerability reporting guidelines present." if has_security else "Missing SECURITY.md policy.",
        },
    ]

    missing_criteria = [c for c in criteria if not c["passed"]]

    return {
        "total_score": total_score,
        "level_class": level_class,
        "level_label": level_label,
        "criteria": criteria,
        "missing_criteria": missing_criteria,
    }
