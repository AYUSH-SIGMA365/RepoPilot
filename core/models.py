from django.db import models

class AnalyzedRepository(models.Model):
    # Identity Fields
    owner = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    url = models.URLField(unique=True)

    # Metadata & Metrics
    description = models.TextField(blank=True, null=True)
    primary_language = models.CharField(max_length=100, blank=True, null=True)
    languages = models.JSONField(default=dict)

    stars_count = models.IntegerField(default=0, db_index=True)
    forks_count = models.IntegerField(default=0)
    open_issues_count = models.IntegerField(default=0, null=True, blank=True)
    open_prs_count = models.IntegerField(default=0)
    has_issues = models.BooleanField(default=True)
    issues_count_is_stale = models.BooleanField(default=False)
    issue_tracker_url = models.URLField(max_length=500, blank=True, null=True)
    releases_count = models.IntegerField(default=0)
    tags_count = models.IntegerField(default=0)
    last_activity_at = models.DateTimeField(null=True, blank=True)

    # Community Standards
    has_readme = models.BooleanField(default=False)
    has_license = models.BooleanField(default=False)
    has_contributing = models.BooleanField(default=False)
    has_code_of_conduct = models.BooleanField(default=False)
    has_security = models.BooleanField(default=False)

    # System Fields & Calculated Data
    readiness_score = models.FloatField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['owner', 'name'], name='unique_repo_owner_name')
        ]

    def __str__(self):
        return f"{self.owner}/{self.name}"

    def get_readiness_breakdown(self):
        from .scoring import calculate_readiness_score
        return calculate_readiness_score(self)

    @property
    def display_issues_count(self) -> str:
        """
        Returns the formatted issue-only count:
        - 'N/A' if GitHub issues are disabled.
        - 'Unavailable' if GitHub issues are enabled but count is None.
        - '{count} (Stale)' if issues_count_is_stale is True.
        - The integer string (e.g., '1737', '0') if GitHub issues are verified.
        """
        if not self.has_issues:
            return "N/A"
        if self.open_issues_count is None:
            return "Unavailable"
        if self.issues_count_is_stale:
            return f"{self.open_issues_count} (Stale)"
        return str(self.open_issues_count)

    @property
    def issues_label(self) -> str:
        """
        Returns 'Open Issues' if GitHub issues are verified,
        'Open Issues (Stale)' if the count is preserved but stale,
        'Open Issues (Unavailable)' if the count is unavailable,
        or 'Issues (External/N/A)' if GitHub issues are disabled.
        """
        if not self.has_issues:
            return "Issues (External/N/A)"
        if self.issues_count_is_stale:
            return "Open Issues (Stale)"
        if self.open_issues_count is None:
            return "Open Issues (Unavailable)"
        return "Open Issues"


class OnboardingProgress(models.Model):
    """
    Persists onboarding progress for a specific browser session and repository.
    Enforces session isolation without requiring a full user authentication system.
    """
    session_key = models.CharField(max_length=40, db_index=True)
    repo_owner = models.CharField(max_length=255)
    repo_name = models.CharField(max_length=255)
    repository = models.ForeignKey(
        AnalyzedRepository,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="mentor_progress",
    )
    completed_steps = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["session_key", "repo_owner", "repo_name"],
                name="unique_session_repo_progress",
            )
        ]

    def __str__(self):
        return f"{self.session_key} - {self.repo_owner}/{self.repo_name}"

    def get_completed_count(self) -> int:
        return len(self.completed_steps) if isinstance(self.completed_steps, list) else 0

    def is_step_completed(self, step_id: str) -> bool:
        if isinstance(self.completed_steps, list):
            return step_id in self.completed_steps
        return False
