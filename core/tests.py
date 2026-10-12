import urllib.error
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch, MagicMock
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from .models import AnalyzedRepository
from .scoring import calculate_readiness_score
from .github_service import (
    parse_github_url,
    analyze_and_save_repository,
    _fetch_collection_count,
    InvalidURLError,
    RepositoryNotFoundError,
    RateLimitError,
    NetworkError,
)


class GitHubServiceURLParsingTests(TestCase):
    def test_valid_urls(self):
        cases = [
            ("https://github.com/pallets/flask", ("pallets", "flask")),
            ("http://github.com/django/django/", ("django", "django")),
            ("github.com/psf/black", ("psf", "black")),
            ("https://github.com/python/cpython.git", ("python", "cpython")),
            ("https://www.github.com/facebook/react", ("facebook", "react")),
        ]
        for url, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(parse_github_url(url), expected)

    def test_invalid_urls(self):
        invalid_cases = [
            "",
            "   ",
            "https://gitlab.com/pallets/flask",
            "https://github.com/pallets",
            "https://github.com/",
            "not-a-valid-url",
            "https://github.com/owner/repo/extra/path",
        ]
        for url in invalid_cases:
            with self.subTest(url=url):
                with self.assertRaises(InvalidURLError):
                    parse_github_url(url)


class GitHubCollectionCountTests(TestCase):
    @patch("core.github_service._make_github_request")
    def test_empty_collection(self, mock_req):
        mock_req.return_value = ([], {})
        count = _fetch_collection_count("https://api.github.com/repos/owner/repo/releases")
        self.assertEqual(count, 0)

    @patch("core.github_service._make_github_request")
    def test_single_item_no_link_header(self, mock_req):
        mock_req.return_value = ([{"id": 1}], {})
        count = _fetch_collection_count("https://api.github.com/repos/owner/repo/releases")
        self.assertEqual(count, 1)

    @patch("core.github_service._make_github_request")
    def test_paginated_collection_link_header(self, mock_req):
        headers = {
            "Link": '<https://api.github.com/repos/owner/repo/releases?per_page=1&page=2>; rel="next", <https://api.github.com/repos/owner/repo/releases?per_page=1&page=145>; rel="last"'
        }
        mock_req.return_value = ([{"id": 1}], headers)
        count = _fetch_collection_count("https://api.github.com/repos/owner/repo/releases")
        self.assertEqual(count, 145)


    @patch("core.github_service._make_github_request")
    def test_tags_collection_link_header(self, mock_req):
        headers = {
            "Link": '<https://api.github.com/repos/owner/repo/tags?per_page=1&page=2>; rel="next", <https://api.github.com/repos/owner/repo/tags?per_page=1&page=522>; rel="last"'
        }
        mock_req.return_value = ([{"name": "v1.0"}], headers)
        count = _fetch_collection_count("https://api.github.com/repos/owner/repo/tags")
        self.assertEqual(count, 522)


class GitHubServiceDataFetchAndSaveTests(TestCase):
    def setUp(self):
        AnalyzedRepository.objects.filter(owner__in=["mock-owner", "pallets-test"]).delete()

    def tearDown(self):
        AnalyzedRepository.objects.filter(owner__in=["mock-owner", "pallets-test"]).delete()

    @patch("core.github_service._fetch_collection_count")
    @patch("core.github_service._make_github_request")
    def test_issues_prs_releases_and_tags_separation(self, mock_request, mock_count):
        # 1. Base repo returns 50 raw open issues (which in GitHub includes PRs)
        def req_side_effect(url, return_headers=False):
            if "languages" in url:
                return {"Python": 120000}
            elif "community/profile" in url:
                return {
                    "files": {
                        "readme": {"url": "readme"},
                        "license": {"url": "license"},
                        "contributing": {"url": "contributing"},
                        "code_of_conduct": None,
                        "security": None,
                    }
                }
            else:
                return {
                    "description": "Mocked test repository.",
                    "language": "Python",
                    "stargazers_count": 5000,
                    "forks_count": 800,
                    "open_issues_count": 50,  # Raw GitHub count (issues + PRs)
                    "pushed_at": "2026-10-10T12:00:00Z",
                    "license": {"name": "MIT"},
                }

        mock_request.side_effect = req_side_effect

        # 2. Mock collections: 15 open PRs, 0 releases, 522 tags
        def count_side_effect(url):
            if "pulls" in url:
                return 15
            elif "releases" in url:
                return 0  # e.g., Django has 0 GitHub releases
            elif "tags" in url:
                return 522  # e.g., Django has 522 Git tags
            return 0

        mock_count.side_effect = count_side_effect

        repo = analyze_and_save_repository("https://github.com/mock-owner/mock-repo")

        # Verify separation: actual issues = 50 - 15 = 35
        self.assertEqual(repo.open_prs_count, 15)
        self.assertEqual(repo.open_issues_count, 35)

        # Verify releases and tags are strictly separate
        self.assertEqual(repo.releases_count, 0)
        self.assertEqual(repo.tags_count, 522)

        # Verify standard flags
        self.assertEqual(repo.stars_count, 5000)
        self.assertTrue(repo.has_readme)
        self.assertTrue(repo.has_license)

    @patch("core.github_service._fetch_collection_count")
    @patch("core.github_service._make_github_request")
    def test_idempotent_update_existing_repo(self, mock_request, mock_count):
        AnalyzedRepository.objects.create(
            owner="pallets-test",
            name="flask",
            url="https://github.com/pallets-test/flask",
            stars_count=100,
            open_issues_count=5,
            open_prs_count=2,
        )

        mock_request.side_effect = lambda url, return_headers=False: (
            {"Python": 2000} if "languages" in url else (
                {"files": {}} if "community/profile" in url else {
                    "description": "Updated description",
                    "language": "Python",
                    "stargazers_count": 300,
                    "forks_count": 60,
                    "open_issues_count": 20,
                    "pushed_at": "2026-10-12T00:00:00Z",
                    "license": None,
                }
            )
        )
        mock_count.side_effect = lambda url: 5 if "pulls" in url else 10

        repo = analyze_and_save_repository("https://github.com/pallets-test/flask")

        # Must update the existing row, keeping exactly 1 row for this repo
        self.assertEqual(AnalyzedRepository.objects.filter(owner="pallets-test", name="flask").count(), 1)
        self.assertEqual(repo.stars_count, 300)
        self.assertEqual(repo.open_issues_count, 15)  # 20 - 5
        self.assertEqual(repo.open_prs_count, 5)
        self.assertEqual(repo.releases_count, 10)

    @patch("core.github_service._make_github_request")
    def test_nonexistent_repo_raises_not_found(self, mock_request):
        mock_request.return_value = None
        with self.assertRaises(RepositoryNotFoundError):
            analyze_and_save_repository("https://github.com/mock-owner/nonexistent-repo")

    @patch("core.github_service.urllib.request.urlopen")
    def test_rate_limit_error_handling(self, mock_urlopen):
        err = urllib.error.HTTPError(
            url="https://api.github.com",
            code=403,
            msg="rate limit exceeded",
            hdrs={},
            fp=None,
        )
        mock_urlopen.side_effect = err
        with self.assertRaises(RateLimitError):
            analyze_and_save_repository("https://github.com/mock-owner/rate-limit-repo")

    @patch("core.github_service.urllib.request.urlopen")
    def test_network_error_handling(self, mock_urlopen):
        err = urllib.error.URLError(reason="Connection timed out")
        mock_urlopen.side_effect = err
        with self.assertRaises(NetworkError):
            analyze_and_save_repository("https://github.com/mock-owner/network-err-repo")


class HomeViewTests(TestCase):
    def setUp(self):
        self.client = Client(SERVER_NAME="localhost")

    def test_home_page_get(self):
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Analyze Any Public GitHub Repository")

    def test_home_page_post_invalid_url(self):
        response = self.client.post(reverse("home"), {"repo_url": "invalid-url"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid host")

    @patch("core.views.analyze_and_save_repository")
    def test_home_page_post_valid_url_displays_issues_and_prs(self, mock_analyze):
        mock_repo = AnalyzedRepository(
            owner="django",
            name="django",
            url="https://github.com/django/django",
            primary_language="Python",
            stars_count=80000,
            open_issues_count=180,
            open_prs_count=45,
            releases_count=0,
            tags_count=522,
            has_readme=True,
            has_license=True,
        )
        mock_analyze.return_value = mock_repo

        response = self.client.post(reverse("home"), {"repo_url": "https://github.com/django/django"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "django/django")
        self.assertContains(response, "Open Issues")
        self.assertContains(response, "180")
        self.assertContains(response, "Open PRs")
        self.assertContains(response, "45")
        self.assertContains(response, "Releases")
        self.assertContains(response, "0")
        self.assertContains(response, "Version Tags")
        self.assertContains(response, "522")

    @patch("core.views.analyze_and_save_repository")
    def test_dashboard_displays_readiness_score_and_breakdown(self, mock_analyze):
        fixed_now = datetime(2026, 10, 13, 12, 0, 0, tzinfo=dt_timezone.utc)
        mock_repo = AnalyzedRepository(
            owner="psf",
            name="black",
            url="https://github.com/psf/black",
            description="The uncompromising Python code formatter",
            primary_language="Python",
            stars_count=35000,
            open_issues_count=120,
            open_prs_count=15,
            releases_count=30,
            tags_count=45,
            has_readme=True,
            has_license=True,
            has_contributing=True,
            has_code_of_conduct=True,
            has_security=False,  # Missing 5 pts -> Total 95 pts (with recent activity)
            last_activity_at=fixed_now - timedelta(days=10),
            readiness_score=95,
        )
        mock_analyze.return_value = mock_repo

        with patch("core.scoring.timezone.now", return_value=fixed_now):
            response = self.client.post(reverse("home"), {"repo_url": "https://github.com/psf/black"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Repository Readiness Score")
        self.assertContains(response, "95")
        self.assertContains(response, "/100")
        self.assertContains(response, "High Readiness")
        self.assertContains(response, "Readiness Criteria Breakdown")
        self.assertContains(response, "README Documentation")
        self.assertContains(response, "CONTRIBUTING Guide")
        self.assertContains(response, "Open Source License")
        self.assertContains(response, "Recent Repository Activity")
        self.assertContains(response, "Primary Programming Language")
        self.assertContains(response, "Code of Conduct")
        self.assertContains(response, "Repository Description")
        self.assertContains(response, "Security Policy")
        self.assertContains(response, "Missing Criteria for New Contributors")
        self.assertContains(response, "measures contributor onboarding readiness, not code or project quality")


class RepositoryReadinessScoringTests(TestCase):
    def setUp(self):
        self.fixed_now = datetime(2026, 10, 13, 12, 0, 0, tzinfo=dt_timezone.utc)

    def test_all_criteria_satisfied_produces_100(self):
        data = {
            "has_readme": True,
            "has_license": True,
            "has_contributing": True,
            "has_code_of_conduct": True,
            "has_security": True,
            "description": "Comprehensive open-source repository",
            "primary_language": "Python",
            "last_activity_at": self.fixed_now - timedelta(days=2),
        }
        res = calculate_readiness_score(data, now=self.fixed_now)
        self.assertEqual(res["total_score"], 100)
        self.assertEqual(res["level_class"], "success")
        self.assertEqual(res["level_label"], "High Readiness")
        self.assertEqual(len(res["missing_criteria"]), 0)
        self.assertEqual(len(res["criteria"]), 8)

    def test_all_criteria_unsatisfied_produces_0(self):
        data = {
            "has_readme": False,
            "has_license": False,
            "has_contributing": False,
            "has_code_of_conduct": False,
            "has_security": False,
            "description": "",
            "primary_language": "",
            "last_activity_at": None,
        }
        res = calculate_readiness_score(data, now=self.fixed_now)
        self.assertEqual(res["total_score"], 0)
        self.assertEqual(res["level_class"], "danger")
        self.assertEqual(res["level_label"], "Low Readiness")
        self.assertEqual(len(res["missing_criteria"]), 8)

    def test_documentation_and_community_criteria_independently(self):
        # 1. README alone (20 pts)
        res = calculate_readiness_score({"has_readme": True}, now=self.fixed_now)
        self.assertEqual(res["total_score"], 20)

        # 2. License alone (15 pts)
        res = calculate_readiness_score({"has_license": True}, now=self.fixed_now)
        self.assertEqual(res["total_score"], 15)

        # 3. CONTRIBUTING alone (20 pts)
        res = calculate_readiness_score({"has_contributing": True}, now=self.fixed_now)
        self.assertEqual(res["total_score"], 20)

        # 4. Code of Conduct alone (10 pts)
        res = calculate_readiness_score({"has_code_of_conduct": True}, now=self.fixed_now)
        self.assertEqual(res["total_score"], 10)

        # 5. Security policy alone (5 pts)
        res = calculate_readiness_score({"has_security": True}, now=self.fixed_now)
        self.assertEqual(res["total_score"], 5)

    def test_description_and_primary_language_scoring(self):
        # Description: non-empty -> 5 pts
        res = calculate_readiness_score({"description": "A valid description."}, now=self.fixed_now)
        self.assertEqual(res["total_score"], 5)

        # Description: whitespace only -> 0 pts
        res = calculate_readiness_score({"description": "   \n\t  "}, now=self.fixed_now)
        self.assertEqual(res["total_score"], 0)

        # Description: None -> 0 pts
        res = calculate_readiness_score({"description": None}, now=self.fixed_now)
        self.assertEqual(res["total_score"], 0)

        # Primary language: non-empty -> 10 pts
        res = calculate_readiness_score({"primary_language": "Python"}, now=self.fixed_now)
        self.assertEqual(res["total_score"], 10)

        # Primary language: whitespace only -> 0 pts
        res = calculate_readiness_score({"primary_language": "   "}, now=self.fixed_now)
        self.assertEqual(res["total_score"], 0)

        # Primary language: None -> 0 pts
        res = calculate_readiness_score({"primary_language": None}, now=self.fixed_now)
        self.assertEqual(res["total_score"], 0)

    def test_activity_within_30_days(self):
        res = calculate_readiness_score(
            {"last_activity_at": self.fixed_now - timedelta(days=15)},
            now=self.fixed_now,
        )
        self.assertEqual(res["total_score"], 15)

    def test_activity_31_to_90_days_ago(self):
        res = calculate_readiness_score(
            {"last_activity_at": self.fixed_now - timedelta(days=50)},
            now=self.fixed_now,
        )
        self.assertEqual(res["total_score"], 10)

    def test_activity_91_to_180_days_ago(self):
        res = calculate_readiness_score(
            {"last_activity_at": self.fixed_now - timedelta(days=120)},
            now=self.fixed_now,
        )
        self.assertEqual(res["total_score"], 5)

    def test_activity_older_than_180_days(self):
        res = calculate_readiness_score(
            {"last_activity_at": self.fixed_now - timedelta(days=200)},
            now=self.fixed_now,
        )
        self.assertEqual(res["total_score"], 0)

    def test_missing_activity_date(self):
        res = calculate_readiness_score({"last_activity_at": None}, now=self.fixed_now)
        self.assertEqual(res["total_score"], 0)

    def test_activity_date_boundary_cases(self):
        # 0 days ago (exact now) -> 15 pts
        res = calculate_readiness_score({"last_activity_at": self.fixed_now}, now=self.fixed_now)
        self.assertEqual(res["total_score"], 15)

        # Exactly 30 days ago -> 15 pts
        res = calculate_readiness_score(
            {"last_activity_at": self.fixed_now - timedelta(days=30)},
            now=self.fixed_now,
        )
        self.assertEqual(res["total_score"], 15)

        # Exactly 31 days ago -> 10 pts
        res = calculate_readiness_score(
            {"last_activity_at": self.fixed_now - timedelta(days=31)},
            now=self.fixed_now,
        )
        self.assertEqual(res["total_score"], 10)

        # Exactly 90 days ago -> 10 pts
        res = calculate_readiness_score(
            {"last_activity_at": self.fixed_now - timedelta(days=90)},
            now=self.fixed_now,
        )
        self.assertEqual(res["total_score"], 10)

        # Exactly 91 days ago -> 5 pts
        res = calculate_readiness_score(
            {"last_activity_at": self.fixed_now - timedelta(days=91)},
            now=self.fixed_now,
        )
        self.assertEqual(res["total_score"], 5)

        # Exactly 180 days ago -> 5 pts
        res = calculate_readiness_score(
            {"last_activity_at": self.fixed_now - timedelta(days=180)},
            now=self.fixed_now,
        )
        self.assertEqual(res["total_score"], 5)

        # Exactly 181 days ago -> 0 pts
        res = calculate_readiness_score(
            {"last_activity_at": self.fixed_now - timedelta(days=181)},
            now=self.fixed_now,
        )
        self.assertEqual(res["total_score"], 0)

    @patch("core.github_service._fetch_collection_count")
    @patch("core.github_service._make_github_request")
    def test_score_recalculation_when_repository_information_changes(self, mock_request, mock_count):
        # Clean up test repo
        AnalyzedRepository.objects.filter(owner="score-test", name="repo").delete()

        # Initial analysis: only readme and primary language present (20 + 10 = 30)
        mock_request.side_effect = lambda url, return_headers=False: (
            {"Python": 500} if "languages" in url else (
                {"files": {"readme": {"url": "readme"}}} if "community/profile" in url else {
                    "description": "",
                    "language": "Python",
                    "stargazers_count": 10,
                    "forks_count": 2,
                    "open_issues_count": 1,
                    "pushed_at": "2020-01-01T00:00:00Z",  # very old -> 0 pts
                    "license": None,
                }
            )
        )
        mock_count.side_effect = lambda url: 0

        repo = analyze_and_save_repository("https://github.com/score-test/repo")
        self.assertEqual(repo.readiness_score, 30)

        # Second analysis / refresh: project adds license, contributing guide, code of conduct,
        # description, and recent activity
        recent_pushed = (timezone.now() - timedelta(days=5)).isoformat()
        mock_request.side_effect = lambda url, return_headers=False: (
            {"Python": 500} if "languages" in url else (
                {
                    "files": {
                        "readme": {"url": "readme"},
                        "license": {"url": "license"},
                        "contributing": {"url": "contributing"},
                        "code_of_conduct": {"url": "coc"},
                        "security": None,
                    }
                } if "community/profile" in url else {
                    "description": "Now with great docs!",
                    "language": "Python",
                    "stargazers_count": 50,
                    "forks_count": 10,
                    "open_issues_count": 2,
                    "pushed_at": recent_pushed,
                    "license": {"name": "MIT"},
                }
            )
        )

        repo_updated = analyze_and_save_repository("https://github.com/score-test/repo")

        # Recalculated score: README (20) + License (15) + Contributing (20) + CoC (10) +
        # Description (5) + Language (10) + Activity (15) = 95
        self.assertEqual(repo_updated.readiness_score, 95)
        # Verify persistence in database
        persisted = AnalyzedRepository.objects.get(owner="score-test", name="repo")
        self.assertEqual(persisted.readiness_score, 95)

        # Cleanup
        AnalyzedRepository.objects.filter(owner="score-test", name="repo").delete()
