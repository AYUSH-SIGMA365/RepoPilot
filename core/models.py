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
    open_issues_count = models.IntegerField(default=0)
    open_prs_count = models.IntegerField(default=0)
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
