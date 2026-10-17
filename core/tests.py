import urllib.error
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch, MagicMock
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from .models import AnalyzedRepository, OnboardingProgress
from .checklist import generate_onboarding_checklist
from .scoring import calculate_readiness_score
from .github_service import (
    parse_github_url,
    analyze_and_save_repository,
    search_github_repositories,
    fetch_repository_documentation,
    fetch_github_metadata,
    _fetch_collection_count,
    _make_github_request,
    _extract_external_tracker_url,
    InvalidURLError,
    RepositoryNotFoundError,
    RateLimitError,
    NetworkError,
    AuthenticationError,
    GitHubServiceError,
)
from .issue_service import (
    parse_github_issue_url,
    fetch_github_issue,
    extract_issue_evidence,
    generate_contribution_plan,
    analyze_issue_and_generate_plan,
    IssueNotFoundError,
    PullRequestNotSupportedError,
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


class GitHubServiceRequestAndRetryTests(TestCase):
    @patch("core.github_service.urllib.request.urlopen")
    def test_request_success_first_try(self, mock_urlopen):
        import json
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"name": "RepoPilot"}).encode("utf-8")
        mock_resp.headers = {"Link": ""}
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        data, headers = _make_github_request("https://api.github.com/repos/test/repo", return_headers=True)
        self.assertEqual(data["name"], "RepoPilot")
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("core.github_service.time.sleep")
    @patch("core.github_service.urllib.request.urlopen")
    def test_request_retry_on_transient_url_error(self, mock_urlopen, mock_sleep):
        import json
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"status": "ok"}).encode("utf-8")
        mock_resp.headers = {}
        mock_resp.__enter__.return_value = mock_resp

        mock_urlopen.side_effect = [
            urllib.error.URLError("Connection reset by peer"),
            mock_resp,
        ]

        data = _make_github_request("https://api.github.com/repos/test/transient")
        self.assertEqual(data, {"status": "ok"})
        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once()

    @patch("core.github_service.time.sleep")
    @patch("core.github_service.urllib.request.urlopen")
    def test_request_retry_on_ssl_timeout(self, mock_urlopen, mock_sleep):
        import json
        import ssl
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"status": "ok"}).encode("utf-8")
        mock_resp.headers = {}
        mock_resp.__enter__.return_value = mock_resp

        mock_urlopen.side_effect = [
            ssl.SSLError("The handshake operation timed out"),
            mock_resp,
        ]

        data = _make_github_request("https://api.github.com/repos/test/ssl-timeout")
        self.assertEqual(data, {"status": "ok"})
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("core.github_service.time.sleep")
    @patch("core.github_service.urllib.request.urlopen")
    def test_request_exhaustion_raises_network_error(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = urllib.error.URLError("The handshake operation timed out")

        with self.assertRaises(NetworkError) as ctx:
            _make_github_request("https://api.github.com/repos/test/persistent-timeout")
        self.assertIn("handshake operation timed out", str(ctx.exception))
        self.assertEqual(mock_urlopen.call_count, 3)

    @patch("core.github_service.urllib.request.urlopen")
    def test_request_401_raises_authentication_error(self, mock_urlopen):
        err = urllib.error.HTTPError(
            url="https://api.github.com",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=None,
        )
        mock_urlopen.side_effect = err
        with self.assertRaises(AuthenticationError):
            _make_github_request("https://api.github.com/repos/test/auth-fail")
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("core.github_service.urllib.request.urlopen")
    def test_request_404_returns_none(self, mock_urlopen):
        err = urllib.error.HTTPError(
            url="https://api.github.com",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=None,
        )
        mock_urlopen.side_effect = err
        res = _make_github_request("https://api.github.com/repos/test/notfound")
        self.assertIsNone(res)
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("core.github_service.urllib.request.urlopen")
    def test_request_403_rate_limit(self, mock_urlopen):
        err = urllib.error.HTTPError(
            url="https://api.github.com",
            code=403,
            msg="API rate limit exceeded",
            hdrs={},
            fp=None,
        )
        mock_urlopen.side_effect = err
        with self.assertRaises(RateLimitError):
            _make_github_request("https://api.github.com/repos/test/rate-limited")
        self.assertEqual(mock_urlopen.call_count, 1)


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
            if "search/issues" in url:
                return {"total_count": 35}
            elif "languages" in url:
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

        # Verify separation: actual issues = 35 from Search API
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
            {"total_count": 15} if "search/issues" in url else (
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


class ExternalTrackerDiscoveryTests(TestCase):
    def test_extract_tracker_trac(self):
        text = "Please report bugs and submit patches at https://code.djangoproject.com/newticket for Django."
        found = _extract_external_tracker_url([text])
        self.assertEqual(found, "https://code.djangoproject.com/newticket")

    def test_extract_tracker_jira(self):
        text = "Issues are tracked in Jira: https://issues.apache.org/jira/browse/KAFKA"
        found = _extract_external_tracker_url([text])
        self.assertEqual(found, "https://issues.apache.org/jira/browse/KAFKA")

    def test_extract_tracker_bugzilla(self):
        text = "Submit tickets to https://bugs.kde.org/enter_bug.cgi"
        found = _extract_external_tracker_url([text])
        self.assertEqual(found, "https://bugs.kde.org/enter_bug.cgi")

    def test_extract_tracker_ignores_irrelevant_urls(self):
        candidates = [
            "Check https://docs.djangoproject.com/en/stable/ and https://github.com/django/django for info.",
            "Visit https://www.python.org or https://twitter.com/django",
        ]
        self.assertIsNone(_extract_external_tracker_url(candidates))

    def test_extract_tracker_ignores_arbitrary_issue_or_ticket_words(self):
        candidates = [
            "See our blog at https://example.com/known-issues for details.",
            "Purchase admission at https://events.org/buy-tickets/ now.",
            "Read documentation at https://acme.org/article-about-issue-tracking",
        ]
        self.assertIsNone(_extract_external_tracker_url(candidates))

    def test_extract_tracker_strips_trailing_punctuation(self):
        text = "Report tickets here: (https://code.djangoproject.com/newticket), or check FAQ."
        found = _extract_external_tracker_url([text])
        self.assertEqual(found, "https://code.djangoproject.com/newticket")

    def test_extract_tracker_empty_or_none(self):
        self.assertIsNone(_extract_external_tracker_url([]))
        self.assertIsNone(_extract_external_tracker_url([None, ""]))


class GitHubServiceDisabledIssuesTests(TestCase):
    def setUp(self):
        AnalyzedRepository.objects.filter(owner="test-issues").delete()

    def tearDown(self):
        AnalyzedRepository.objects.filter(owner="test-issues").delete()

    @patch("core.github_service._fetch_collection_count")
    @patch("core.github_service._make_github_request")
    def test_fetch_metadata_disabled_issues_sets_none(self, mock_request, mock_count):
        mock_request.side_effect = lambda url, return_headers=False: (
            {"Python": 50000} if "languages" in url else (
                {"files": {}} if "community/profile" in url else {
                    "description": "Repo using Trac: https://code.djangoproject.com/newticket",
                    "homepage": "https://www.djangoproject.com/",
                    "language": "Python",
                    "stargazers_count": 80000,
                    "forks_count": 32000,
                    "has_issues": False,
                    "open_issues_count": 541,
                    "pushed_at": "2026-10-12T00:00:00Z",
                    "license": {"name": "BSD-3-Clause"},
                }
            )
        )
        mock_count.side_effect = lambda url: 535 if "pulls" in url else 10

        meta = fetch_github_metadata("test-issues", "no-issues-repo")
        self.assertFalse(meta["has_issues"])
        self.assertIsNone(meta["open_issues_count"])
        self.assertEqual(meta["open_prs_count"], 535)
        self.assertEqual(meta["issue_tracker_url"], "https://code.djangoproject.com/newticket")

    @patch("core.github_service._fetch_collection_count")
    @patch("core.github_service._make_github_request")
    def test_fetch_metadata_enabled_issues_authoritative_search_count(self, mock_request, mock_count):
        mock_request.side_effect = lambda url, return_headers=False: (
            {"total_count": 35} if "search/issues" in url else (
                {"Python": 50000} if "languages" in url else (
                    {"files": {}} if "community/profile" in url else {
                        "description": "Standard repo with GitHub issues",
                        "homepage": "https://example.com",
                        "language": "Python",
                        "stargazers_count": 100,
                        "forks_count": 20,
                        "has_issues": True,
                        "open_issues_count": 50,
                        "pushed_at": "2026-10-12T00:00:00Z",
                        "license": None,
                    }
                )
            )
        )
        mock_count.side_effect = lambda url: 15 if "pulls" in url else 5

        meta = fetch_github_metadata("test-issues", "standard-repo")
        self.assertTrue(meta["has_issues"])
        self.assertEqual(meta["open_issues_count"], 35)
        self.assertEqual(meta["open_prs_count"], 15)
        self.assertEqual(meta["issue_tracker_url"], "https://github.com/test-issues/standard-repo/issues?q=is%3Aissue+is%3Aopen")

    @patch("core.github_service._fetch_collection_count")
    @patch("core.github_service._make_github_request")
    def test_reanalyze_refreshes_has_issues_and_open_issues_count(self, mock_request, mock_count):
        repo = AnalyzedRepository.objects.create(
            owner="test-issues",
            name="toggle-issues-repo",
            url="https://github.com/test-issues/toggle-issues-repo",
            stars_count=1000,
            has_issues=True,
            open_issues_count=6,
            open_prs_count=500,
        )

        mock_request.side_effect = lambda url, return_headers=False: (
            {"Python": 1000} if "languages" in url else (
                {"files": {}} if "community/profile" in url else {
                    "description": "Updated to disabled issues",
                    "homepage": "https://bugs.kde.org/enter_bug.cgi",
                    "language": "Python",
                    "stargazers_count": 1050,
                    "forks_count": 200,
                    "has_issues": False,
                    "open_issues_count": 510,
                    "pushed_at": "2026-10-13T00:00:00Z",
                    "license": None,
                }
            )
        )
        mock_count.side_effect = lambda url: 510 if "pulls" in url else 0

        updated_repo = analyze_and_save_repository("https://github.com/test-issues/toggle-issues-repo")
        self.assertEqual(updated_repo.id, repo.id)
        self.assertFalse(updated_repo.has_issues)
        self.assertIsNone(updated_repo.open_issues_count)
        self.assertEqual(updated_repo.issue_tracker_url, "https://bugs.kde.org/enter_bug.cgi")


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

    @patch("core.views.analyze_and_save_repository")
    def test_home_page_post_disabled_issues_displays_na(self, mock_analyze):
        mock_repo = AnalyzedRepository(
            owner="django",
            name="django",
            url="https://github.com/django/django",
            primary_language="Python",
            stars_count=80000,
            has_issues=False,
            open_issues_count=None,
            open_prs_count=540,
            releases_count=0,
            tags_count=522,
            has_readme=True,
            has_license=True,
        )
        mock_analyze.return_value = mock_repo

        response = self.client.post(reverse("home"), {"repo_url": "https://github.com/django/django"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Issues (External/N/A)")
        self.assertContains(response, "N/A")


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


class OSSNavigatorSearchServiceTests(TestCase):
    @patch("core.github_service._make_github_request")
    def test_search_query_builder_with_all_filters(self, mock_request):
        mock_request.return_value = ({"total_count": 5, "items": []}, {})

        search_github_repositories(
            query="cli tool",
            language="rust",
            topic="terminal",
            beginner_friendly=True,
            min_stars=150,
            page=2,
            per_page=10,
        )

        mock_request.assert_called_once()
        called_url = mock_request.call_args[0][0]
        # Verify query string parameters and qualifiers
        self.assertIn("is%3Apublic", called_url)
        self.assertIn("archived%3Afalse", called_url)
        self.assertIn("cli+tool", called_url)
        self.assertIn("language%3Arust", called_url)
        self.assertIn("topic%3Aterminal", called_url)
        self.assertIn("good-first-issues%3A%3E0", called_url)
        self.assertIn("stars%3A%3E%3D150", called_url)
        self.assertIn("page=2", called_url)
        self.assertIn("per_page=10", called_url)

    @patch("core.github_service._make_github_request")
    def test_search_query_builder_defaults(self, mock_request):
        mock_request.return_value = ({"total_count": 0, "items": []}, {})

        res = search_github_repositories()

        mock_request.assert_called_once()
        called_url = mock_request.call_args[0][0]
        self.assertIn("is%3Apublic", called_url)
        self.assertIn("archived%3Afalse", called_url)
        self.assertIn("stars%3A%3E%3D50", called_url)
        self.assertIn("page=1", called_url)
        self.assertIn("per_page=12", called_url)
        self.assertEqual(res["total_count"], 0)
        self.assertEqual(res["items"], [])

    @patch("core.github_service._make_github_request")
    def test_search_pagination_calculation(self, mock_request):
        mock_request.return_value = ({"total_count": 50, "items": [{"name": f"repo-{i}"} for i in range(10)]}, {})

        # Page 1 of 5
        res_p1 = search_github_repositories(page=1, per_page=10)
        self.assertEqual(res_p1["total_pages"], 5)
        self.assertFalse(res_p1["has_previous"])
        self.assertTrue(res_p1["has_next"])

        # Page 5 of 5
        res_p5 = search_github_repositories(page=5, per_page=10)
        self.assertEqual(res_p5["total_pages"], 5)
        self.assertTrue(res_p5["has_previous"])
        self.assertFalse(res_p5["has_next"])

    @patch("core.github_service._make_github_request")
    def test_search_rate_limit_error(self, mock_request):
        mock_request.side_effect = RateLimitError("GitHub API rate limit exceeded.")
        with self.assertRaises(RateLimitError):
            search_github_repositories()

    @patch("core.github_service._make_github_request")
    def test_search_network_error(self, mock_request):
        mock_request.side_effect = NetworkError("Connection timed out")
        with self.assertRaises(NetworkError):
            search_github_repositories()


class OSSNavigatorViewTests(TestCase):
    def setUp(self):
        self.client = Client(SERVER_NAME="localhost")

    @patch("core.views.search_github_repositories")
    def test_navigator_page_get_default(self, mock_search):
        mock_search.return_value = {
            "total_count": 2,
            "items": [
                {
                    "name": "flask",
                    "full_name": "pallets/flask",
                    "owner": {"login": "pallets", "avatar_url": "https://avatar.url/pallets"},
                    "html_url": "https://github.com/pallets/flask",
                    "description": "Python micro framework",
                    "language": "Python",
                    "stargazers_count": 75000,
                    "forks_count": 16000,
                    "open_issues_count": 20,
                    "topics": ["python", "web", "framework"],
                }
            ],
            "page": 1,
            "per_page": 12,
            "total_pages": 1,
            "has_next": False,
            "has_previous": False,
            "query_used": "is:public archived:false good-first-issues:>0 stars:>=50",
        }

        response = self.client.get(reverse("navigator"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "OSS Navigator")
        self.assertContains(response, "pallets/flask")
        self.assertContains(response, "75000")
        self.assertContains(response, "Apply Filters")

        # Confirm default beginner-friendly filter was passed
        mock_search.assert_called_once()
        call_kwargs = mock_search.call_args[1]
        self.assertTrue(call_kwargs["beginner_friendly"])
        self.assertEqual(call_kwargs["min_stars"], 50)

    @patch("core.views.search_github_repositories")
    def test_navigator_page_with_filters(self, mock_search):
        mock_search.return_value = {
            "total_count": 1,
            "items": [],
            "page": 1,
            "per_page": 12,
            "total_pages": 1,
            "has_next": False,
            "has_previous": False,
            "query_used": "filtered",
        }

        response = self.client.get(reverse("navigator"), {
            "query": "web framework",
            "language": "python",
            "topic": "api",
            "min_stars": "200",
            "beginner_friendly": "on",
        })
        self.assertEqual(response.status_code, 200)
        mock_search.assert_called_once()
        call_kwargs = mock_search.call_args[1]
        self.assertEqual(call_kwargs["query"], "web framework")
        self.assertEqual(call_kwargs["language"], "python")
        self.assertEqual(call_kwargs["topic"], "api")
        self.assertEqual(call_kwargs["min_stars"], 200)
        self.assertTrue(call_kwargs["beginner_friendly"])

    @patch("core.views.search_github_repositories")
    def test_navigator_displays_existing_readiness_score(self, mock_search):
        # Create an existing analyzed repo in PostgreSQL
        AnalyzedRepository.objects.filter(owner="pallets-nav", name="flask").delete()
        AnalyzedRepository.objects.create(
            owner="pallets-nav",
            name="flask",
            url="https://github.com/pallets-nav/flask",
            readiness_score=85,
            has_readme=True,
            has_license=True,
        )

        mock_search.return_value = {
            "total_count": 2,
            "items": [
                {
                    "name": "flask",
                    "full_name": "pallets-nav/flask",
                    "owner": {"login": "pallets-nav", "avatar_url": ""},
                    "html_url": "https://github.com/pallets-nav/flask",
                    "description": "Analyzed repo",
                    "language": "Python",
                    "stargazers_count": 50000,
                    "forks_count": 10000,
                    "open_issues_count": 5,
                    "topics": [],
                },
                {
                    "name": "unexamined-project",
                    "full_name": "unknown-org/unexamined-project",
                    "owner": {"login": "unknown-org", "avatar_url": ""},
                    "html_url": "https://github.com/unknown-org/unexamined-project",
                    "description": "Brand new project",
                    "language": "Go",
                    "stargazers_count": 120,
                    "forks_count": 15,
                    "open_issues_count": 2,
                    "topics": [],
                },
            ],
            "page": 1,
            "per_page": 12,
            "total_pages": 1,
            "has_next": False,
            "has_previous": False,
            "query_used": "test",
        }

        response = self.client.get(reverse("navigator"))
        self.assertEqual(response.status_code, 200)

        # Analyzed repo should display its saved Readiness Score
        self.assertContains(response, "pallets-nav/flask")
        self.assertContains(response, "Readiness: 85/100")

        # Unanalyzed repo should display "Not analyzed" and NOT invent a score
        self.assertContains(response, "unexamined-project")
        self.assertContains(response, "Not analyzed")
        self.assertNotContains(response, "Readiness: 0/100")

        # Cleanup
        AnalyzedRepository.objects.filter(owner="pallets-nav", name="flask").delete()

    @patch("core.views.search_github_repositories")
    def test_navigator_pagination_links_preserve_query_params(self, mock_search):
        mock_search.return_value = {
            "total_count": 50,
            "items": [],
            "page": 1,
            "per_page": 12,
            "total_pages": 5,
            "has_next": True,
            "has_previous": False,
            "query_used": "test",
        }

        response = self.client.get(reverse("navigator"), {
            "language": "python",
            "topic": "web",
            "min_stars": "50",
            "beginner_friendly": "on",
        })
        self.assertEqual(response.status_code, 200)
        # Next link should contain page=2 and preserve other GET parameters
        self.assertContains(response, "page=2")
        self.assertContains(response, "language=python")
        self.assertContains(response, "topic=web")

    @patch("core.views.search_github_repositories")
    def test_navigator_rate_limit_error_display(self, mock_search):
        mock_search.side_effect = RateLimitError("GitHub API rate limit exceeded. Please try again later or add a GITHUB_TOKEN.")

        response = self.client.get(reverse("navigator"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "GitHub API Error")
        self.assertContains(response, "GitHub API rate limit exceeded")

    @patch("core.views.search_github_repositories")
    def test_navigator_empty_results_state(self, mock_search):
        mock_search.return_value = {
            "total_count": 0,
            "items": [],
            "page": 1,
            "per_page": 12,
            "total_pages": 1,
            "has_next": False,
            "has_previous": False,
            "query_used": "no matches",
        }

        response = self.client.get(reverse("navigator"), {"query": "nonexistent_repo_xyz_12345"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No repositories matched your filters")
        self.assertContains(response, "Reset to Default Filters")


class RepositoryDocumentationServiceTests(TestCase):
    @patch("core.github_service._make_github_request")
    def test_fetch_documentation_success(self, mock_req):
        import base64
        readme_raw = "# Welcome to TestRepo\nThis is a sample project for testing."
        encoded_content = base64.b64encode(readme_raw.encode("utf-8")).decode("utf-8")

        mock_req.side_effect = [
            # 1. /readme call
            {"content": encoded_content, "encoding": "base64", "name": "README.md"},
            # 2. /community/profile call
            {
                "files": {
                    "contributing": {"html_url": "https://github.com/owner/testrepo/blob/main/CONTRIBUTING.md"},
                    "code_of_conduct": {"html_url": "https://github.com/owner/testrepo/blob/main/CODE_OF_CONDUCT.md"},
                    "license": {"html_url": "https://github.com/owner/testrepo/blob/main/LICENSE"},
                    "security": {"html_url": "https://github.com/owner/testrepo/blob/main/SECURITY.md"},
                }
            },
        ]

        doc_data = fetch_repository_documentation("owner", "testrepo")
        self.assertTrue(doc_data["has_readme"])
        self.assertIn("Welcome to TestRepo", doc_data["readme_excerpt"])
        self.assertEqual(doc_data["contributing_url"], "https://github.com/owner/testrepo/blob/main/CONTRIBUTING.md")
        self.assertEqual(doc_data["code_of_conduct_url"], "https://github.com/owner/testrepo/blob/main/CODE_OF_CONDUCT.md")
        self.assertEqual(doc_data["license_url"], "https://github.com/owner/testrepo/blob/main/LICENSE")
        self.assertEqual(doc_data["security_url"], "https://github.com/owner/testrepo/blob/main/SECURITY.md")

    @patch("core.github_service._make_github_request")
    def test_fetch_documentation_missing_readme_404(self, mock_req):
        mock_req.side_effect = [
            # 1. /readme call raises 404
            RepositoryNotFoundError("README not found"),
            # 2. /community/profile call
            ({"files": {}}, {}),
        ]

        doc_data = fetch_repository_documentation("owner", "testrepo")
        self.assertFalse(doc_data["has_readme"])
        self.assertEqual(doc_data["readme_excerpt"], "")
        self.assertIsNone(doc_data["contributing_url"])

    @patch("core.github_service._make_github_request")
    def test_fetch_documentation_missing_community_profile_404(self, mock_req):
        import base64
        readme_raw = "Hello world"
        encoded_content = base64.b64encode(readme_raw.encode("utf-8")).decode("utf-8")

        mock_req.side_effect = [
            {"content": encoded_content, "encoding": "base64"},
            RepositoryNotFoundError("Community profile not found"),
        ]

        doc_data = fetch_repository_documentation("owner", "testrepo")
        self.assertTrue(doc_data["has_readme"])
        self.assertIsNone(doc_data["contributing_url"])
        self.assertIsNone(doc_data["code_of_conduct_url"])

    @patch("core.github_service._make_github_request")
    def test_fetch_documentation_rate_limit_error(self, mock_req):
        mock_req.side_effect = RateLimitError("API rate limit exceeded")
        with self.assertRaises(RateLimitError):
            fetch_repository_documentation("owner", "testrepo")

    @patch("core.github_service._make_github_request")
    def test_fetch_documentation_partial_timeout_readme(self, mock_req):
        mock_req.side_effect = [
            NetworkError("handshake operation timed out"),
            {
                "files": {
                    "contributing": {"html_url": "https://github.com/owner/testrepo/blob/main/CONTRIBUTING.md"},
                    "code_of_conduct": None,
                    "license": None,
                    "security": None,
                }
            },
        ]

        doc_data = fetch_repository_documentation("owner", "testrepo")
        self.assertFalse(doc_data["has_readme"])
        self.assertIsNotNone(doc_data["readme_error"])
        self.assertIn("handshake operation timed out", doc_data["readme_error"])
        self.assertIsNone(doc_data["community_error"])
        self.assertEqual(doc_data["contributing_url"], "https://github.com/owner/testrepo/blob/main/CONTRIBUTING.md")

    @patch("core.github_service._make_github_request")
    def test_fetch_documentation_partial_timeout_community_uses_fallback(self, mock_req):
        import base64
        readme_raw = "# TestRepo Docs"
        encoded = base64.b64encode(readme_raw.encode("utf-8")).decode("utf-8")

        mock_req.side_effect = [
            {"content": encoded, "encoding": "base64", "name": "README.md"},
            NetworkError("Connection timed out"),
        ]

        fallback = {
            "has_contributing": True,
            "has_license": True,
        }

        doc_data = fetch_repository_documentation("owner", "testrepo", fallback_standards=fallback)
        self.assertTrue(doc_data["has_readme"])
        self.assertIsNone(doc_data["readme_error"])
        self.assertIsNotNone(doc_data["community_error"])
        self.assertTrue(doc_data["has_contributing"])
        self.assertTrue(doc_data["has_license"])

    @patch("core.github_service._make_github_request")
    def test_fetch_documentation_both_timeout_raises_network_error(self, mock_req):
        mock_req.side_effect = [
            NetworkError("README timeout"),
            NetworkError("Community profile timeout"),
        ]

        with self.assertRaises(NetworkError):
            fetch_repository_documentation("owner", "testrepo")

    @patch("core.github_service._make_github_request")
    def test_fetch_documentation_discovers_tracker_from_readme(self, mock_req):
        import base64
        readme_raw = "Django uses Trac: please submit tickets at https://code.djangoproject.com/newticket"
        encoded = base64.b64encode(readme_raw.encode("utf-8")).decode("utf-8")

        mock_req.side_effect = [
            {"content": encoded, "encoding": "base64", "name": "README.md"},
            {"files": {}},
        ]

        doc_data = fetch_repository_documentation("django", "django", has_issues=False)
        self.assertEqual(doc_data["issue_tracker_url"], "https://code.djangoproject.com/newticket")


class OnboardingChecklistTests(TestCase):
    def setUp(self):
        self.repo_data = {
            "owner": "psf",
            "name": "requests",
            "url": "https://github.com/psf/requests",
            "primary_language": "Python",
            "has_contributing": True,
            "has_code_of_conduct": True,
            "has_license": True,
            "has_security": True,
        }
        self.doc_data = {
            "has_readme": True,
            "readme_excerpt": "# Requests\nA simple HTTP library.",
            "contributing_url": "https://github.com/psf/requests/blob/main/CONTRIBUTING.md",
            "code_of_conduct_url": "https://github.com/psf/requests/blob/main/CODE_OF_CONDUCT.md",
            "license_url": "https://github.com/psf/requests/blob/main/LICENSE",
            "security_url": "https://github.com/psf/requests/blob/main/SECURITY.md",
        }

    def test_generate_checklist_all_ten_steps_present(self):
        checklist = generate_onboarding_checklist(self.repo_data, self.doc_data, completed_step_ids=[])
        self.assertEqual(checklist["total_count"], 10)
        self.assertEqual(checklist["completed_count"], 0)
        self.assertEqual(checklist["remaining_count"], 10)
        self.assertEqual(checklist["progress_percentage"], 0)
        self.assertFalse(checklist["is_all_completed"])

        steps = checklist["steps"]
        self.assertEqual(len(steps), 10)
        for i, step in enumerate(steps, 1):
            self.assertEqual(step["step_number"], i)
            self.assertEqual(step["id"], f"step_{i}")
            self.assertFalse(step["is_completed"])
            self.assertTrue(len(step["title"]) > 0)
            self.assertTrue(len(step["phase"]) > 0)
            self.assertTrue(len(step["guidance"]) > 0)

    def test_generate_checklist_progress_calculation(self):
        # 5 completed steps (50%)
        completed_5 = ["step_1", "step_2", "step_3", "step_4", "step_5"]
        checklist_5 = generate_onboarding_checklist(self.repo_data, self.doc_data, completed_step_ids=completed_5)
        self.assertEqual(checklist_5["completed_count"], 5)
        self.assertEqual(checklist_5["remaining_count"], 5)
        self.assertEqual(checklist_5["progress_percentage"], 50)
        self.assertFalse(checklist_5["is_all_completed"])

        # 10 completed steps (100%)
        completed_10 = [f"step_{i}" for i in range(1, 11)]
        checklist_10 = generate_onboarding_checklist(self.repo_data, self.doc_data, completed_step_ids=completed_10)
        self.assertEqual(checklist_10["completed_count"], 10)
        self.assertEqual(checklist_10["remaining_count"], 0)
        self.assertEqual(checklist_10["progress_percentage"], 100)
        self.assertTrue(checklist_10["is_all_completed"])

    def test_generate_checklist_doc_links_integration(self):
        checklist = generate_onboarding_checklist(self.repo_data, self.doc_data, completed_step_ids=[])
        step_map = {s["id"]: s for s in checklist["steps"]}

        # Step 2: Contributing guide link
        self.assertEqual(step_map["step_2"]["link_url"], "https://github.com/psf/requests/blob/main/CONTRIBUTING.md")
        # Step 8: Code of conduct link
        self.assertEqual(step_map["step_8"]["link_url"], "https://github.com/psf/requests/blob/main/CODE_OF_CONDUCT.md")
        # Step 9: Issues link
        self.assertIn("issues", step_map["step_9"]["link_url"])

    def test_step_9_with_has_issues_true(self):
        repo_data = {
            "owner": "testorg",
            "name": "standard-repo",
            "has_issues": True,
        }
        checklist = generate_onboarding_checklist(repo_data, {}, completed_step_ids=[])
        step_9 = next(s for s in checklist["steps"] if s["id"] == "step_9")
        self.assertIn("is%3Aissue+is%3Aopen", step_9["link_url"])
        self.assertEqual(step_9["link_text"], "Browse Good First Issues on GitHub")

    def test_step_9_with_has_issues_false_and_tracker(self):
        repo_data = {
            "owner": "django",
            "name": "django",
            "has_issues": False,
            "issue_tracker_url": "https://code.djangoproject.com/newticket",
        }
        checklist = generate_onboarding_checklist(repo_data, {}, completed_step_ids=[])
        step_9 = next(s for s in checklist["steps"] if s["id"] == "step_9")
        self.assertEqual(step_9["link_url"], "https://code.djangoproject.com/newticket")
        self.assertEqual(step_9["link_text"], "Browse Official Issue Tracker")

    def test_step_9_with_has_issues_false_no_tracker_has_contributing(self):
        repo_data = {
            "owner": "testorg",
            "name": "no-tracker-repo",
            "has_issues": False,
        }
        doc_data = {
            "contributing_url": "https://github.com/testorg/no-tracker-repo/blob/main/CONTRIBUTING.md",
        }
        checklist = generate_onboarding_checklist(repo_data, doc_data, completed_step_ids=[])
        step_9 = next(s for s in checklist["steps"] if s["id"] == "step_9")
        self.assertEqual(step_9["link_url"], "https://github.com/testorg/no-tracker-repo/blob/main/CONTRIBUTING.md")
        self.assertEqual(step_9["link_text"], "Review Issue Guidelines in CONTRIBUTING")


class OSSMentorViewTests(TestCase):
    def setUp(self):
        self.client = Client(SERVER_NAME="localhost")
        self.test_session_prefix = "test_mentor_session"
        AnalyzedRepository.objects.filter(owner="testmentor").delete()

    def tearDown(self):
        # Ensure test records are cleaned up from PostgreSQL
        OnboardingProgress.objects.filter(session_key__startswith=self.test_session_prefix).delete()
        OnboardingProgress.objects.filter(repo_owner="testmentor").delete()
        AnalyzedRepository.objects.filter(owner="testmentor").delete()

    def test_mentor_empty_state_renders_welcome_guide(self):
        response = self.client.get(reverse("mentor"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "OSS Mentor")
        self.assertContains(response, "Welcome to OSS Mentor")
        self.assertContains(response, "Select or Enter a Public GitHub Repository")

    @patch("core.views.fetch_repository_documentation")
    @patch("core.views.fetch_github_metadata")
    def test_mentor_valid_repo_url(self, mock_meta, mock_doc):
        mock_meta.return_value = {
            "owner": "testmentor",
            "name": "sample-project",
            "url": "https://github.com/testmentor/sample-project",
            "description": "A sample project for mentoring",
            "primary_language": "Python",
            "stars_count": 150,
            "forks_count": 25,
            "open_issues_count": 10,
            "last_activity_at": "2026-10-10T00:00:00Z",
            "has_readme": True,
            "has_contributing": True,
            "has_code_of_conduct": True,
            "has_license": True,
            "has_security_policy": False,
        }
        mock_doc.return_value = {
            "has_readme": True,
            "readme_excerpt": "Sample README text for tests",
            "contributing_url": "https://github.com/testmentor/sample-project/blob/main/CONTRIBUTING.md",
            "code_of_conduct_url": "https://github.com/testmentor/sample-project/blob/main/CODE_OF_CONDUCT.md",
            "license_url": "https://github.com/testmentor/sample-project/blob/main/LICENSE",
            "security_url": None,
        }

        response = self.client.get(reverse("mentor"), {"repo": "testmentor/sample-project"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "sample-project")
        self.assertContains(response, "Actionable Onboarding Steps")
        self.assertContains(response, "Step 1")
        self.assertContains(response, "Step 10")
        self.assertContains(response, "Sample README text for tests")
        self.assertContains(response, "Contribution Preparation Summary")

    def test_mentor_invalid_repo_url_displays_error(self):
        response = self.client.get(reverse("mentor"), {"repo": "invalid@@name!!"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unable to load onboarding guide")

    @patch("core.views.fetch_github_metadata")
    def test_mentor_repository_not_found_error(self, mock_meta):
        mock_meta.side_effect = RepositoryNotFoundError("Repository does not exist")
        response = self.client.get(reverse("mentor"), {"repo": "testmentor/notfound"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unable to load onboarding guide")
        self.assertContains(response, "Repository does not exist")

    @patch("core.views.fetch_github_metadata")
    def test_mentor_rate_limit_error_display(self, mock_meta):
        mock_meta.side_effect = RateLimitError("GitHub API rate limit exceeded")
        response = self.client.get(reverse("mentor"), {"repo": "testmentor/sample"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unable to load onboarding guide")
        self.assertContains(response, "rate limit exceeded")

    def test_mentor_toggle_step_updates_session_progress(self):
        session = self.client.session
        session.save()
        session_key = session.session_key

        # Toggle step_1 on
        response = self.client.post(reverse("mentor_toggle_step"), {
            "step_id": "step_1",
            "repo_owner": "testmentor",
            "repo_name": "sample",
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn("/mentor/?repo=testmentor/sample", response.url)

        # Verify in database
        progress = OnboardingProgress.objects.filter(
            session_key=session_key,
            repo_owner="testmentor",
            repo_name="sample",
        ).first()
        self.assertIsNotNone(progress)
        self.assertIn("step_1", progress.completed_steps)

        # Toggle step_1 off
        response2 = self.client.post(reverse("mentor_toggle_step"), {
            "step_id": "step_1",
            "repo_owner": "testmentor",
            "repo_name": "sample",
        })
        self.assertEqual(response2.status_code, 302)
        progress.refresh_from_db()
        self.assertNotIn("step_1", progress.completed_steps)

    def test_mentor_session_isolation(self):
        # Create client A and client B
        client_a = Client(SERVER_NAME="localhost")
        client_b = Client(SERVER_NAME="localhost")

        session_a = client_a.session
        session_a.save()
        key_a = session_a.session_key

        session_b = client_b.session
        session_b.save()
        key_b = session_b.session_key

        self.assertNotEqual(key_a, key_b)

        # Client A checks step_1 and step_2
        client_a.post(reverse("mentor_toggle_step"), {
            "step_id": "step_1",
            "repo_owner": "testmentor",
            "repo_name": "isolation-test",
        })
        client_a.post(reverse("mentor_toggle_step"), {
            "step_id": "step_2",
            "repo_owner": "testmentor",
            "repo_name": "isolation-test",
        })

        # Client B checks step_3
        client_b.post(reverse("mentor_toggle_step"), {
            "step_id": "step_3",
            "repo_owner": "testmentor",
            "repo_name": "isolation-test",
        })

        progress_a = OnboardingProgress.objects.get(session_key=key_a, repo_owner="testmentor", repo_name="isolation-test")
        progress_b = OnboardingProgress.objects.get(session_key=key_b, repo_owner="testmentor", repo_name="isolation-test")

        # Session A has step_1 and step_2, but NOT step_3
        self.assertEqual(sorted(progress_a.completed_steps), ["step_1", "step_2"])
        # Session B has step_3, but NOT step_1 or step_2
        self.assertEqual(sorted(progress_b.completed_steps), ["step_3"])

    def test_mentor_toggle_invalid_step_ignored(self):
        response = self.client.post(reverse("mentor_toggle_step"), {
            "step_id": "step_999_invalid",
            "repo_owner": "testmentor",
            "repo_name": "sample",
        })
        self.assertEqual(response.status_code, 302)
        # Should not create progress record with invalid step
        self.assertFalse(OnboardingProgress.objects.filter(repo_owner="testmentor", repo_name="sample").exists())

    @patch("core.views.fetch_repository_documentation")
    @patch("core.views.fetch_github_metadata")
    def test_mentor_view_handles_partial_timeout_gracefully(self, mock_meta, mock_doc):
        mock_meta.return_value = {
            "owner": "testmentor",
            "name": "partial-timeout-repo",
            "url": "https://github.com/testmentor/partial-timeout-repo",
            "description": "Repo experiencing partial network timeout",
            "primary_language": "Python",
            "stars_count": 100,
            "forks_count": 20,
            "has_issues": True,
            "open_issues_count": 5,
            "has_readme": True,
            "has_contributing": True,
            "has_code_of_conduct": False,
            "has_license": True,
            "has_security_policy": False,
        }
        mock_doc.return_value = {
            "has_readme": False,
            "readme_excerpt": "",
            "readme_error": "Network error while connecting to GitHub: handshake timed out",
            "community_error": None,
            "contributing_url": "https://github.com/testmentor/partial-timeout-repo/blob/main/CONTRIBUTING.md",
            "code_of_conduct_url": None,
            "license_url": "https://github.com/testmentor/partial-timeout-repo/blob/main/LICENSE",
            "security_url": None,
        }

        response = self.client.get(reverse("mentor"), {"repo": "testmentor/partial-timeout-repo"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Network timeout while fetching README excerpt")
        self.assertContains(response, "Actionable Onboarding Steps")
        self.assertContains(response, "Step 1")
        self.assertContains(response, "Step 10")

    @patch("core.views.fetch_repository_documentation")
    @patch("core.views.fetch_github_metadata")
    def test_mentor_view_disabled_issues_renders_na_and_tracker(self, mock_meta, mock_doc):
        mock_meta.return_value = {
            "owner": "testmentor",
            "name": "disabled-issues-repo",
            "url": "https://github.com/testmentor/disabled-issues-repo",
            "description": "Repo without GitHub issues",
            "primary_language": "Python",
            "stars_count": 500,
            "forks_count": 100,
            "has_issues": False,
            "open_issues_count": None,
            "issue_tracker_url": "https://code.djangoproject.com/newticket",
            "has_readme": True,
            "has_contributing": True,
            "has_code_of_conduct": True,
            "has_license": True,
            "has_security_policy": False,
        }
        mock_doc.return_value = {
            "has_readme": True,
            "readme_excerpt": "Project README",
            "contributing_url": "https://github.com/testmentor/disabled-issues-repo/blob/main/CONTRIBUTING.md",
            "code_of_conduct_url": None,
            "license_url": None,
            "security_url": None,
            "issue_tracker_url": "https://code.djangoproject.com/newticket",
        }

        response = self.client.get(reverse("mentor"), {"repo": "testmentor/disabled-issues-repo"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Issues (External/N/A)")
        self.assertContains(response, "N/A")
        self.assertContains(response, "GitHub Issues are disabled")
        self.assertContains(response, "https://code.djangoproject.com/newticket")
        self.assertNotContains(response, "is%3Aissue+is%3Aopen")

    @patch("core.views.fetch_repository_documentation")
    @patch("core.views.fetch_github_metadata")
    def test_mentor_view_updates_discovered_tracker_in_database(self, mock_meta, mock_doc):
        AnalyzedRepository.objects.create(
            owner="testmentor",
            name="trac-update-repo",
            url="https://github.com/testmentor/trac-update-repo",
            has_issues=False,
            open_issues_count=None,
            issue_tracker_url=None,
            stars_count=100,
        )

        mock_meta.return_value = {
            "owner": "testmentor",
            "name": "trac-update-repo",
            "url": "https://github.com/testmentor/trac-update-repo",
            "description": "Repo without issues",
            "primary_language": "Python",
            "stars_count": 100,
            "forks_count": 20,
            "has_issues": False,
            "open_issues_count": None,
            "issue_tracker_url": None,
            "has_readme": True,
            "has_contributing": True,
            "has_code_of_conduct": False,
            "has_license": True,
            "has_security_policy": False,
        }
        mock_doc.return_value = {
            "has_readme": True,
            "readme_excerpt": "Check out https://code.djangoproject.com/newticket",
            "issue_tracker_url": "https://code.djangoproject.com/newticket",
            "contributing_url": None,
            "code_of_conduct_url": None,
            "license_url": None,
            "security_url": None,
        }

        response = self.client.get(reverse("mentor"), {"repo": "testmentor/trac-update-repo"})
        self.assertEqual(response.status_code, 200)

        repo_in_db = AnalyzedRepository.objects.get(owner="testmentor", name="trac-update-repo")
        self.assertEqual(repo_in_db.issue_tracker_url, "https://code.djangoproject.com/newticket")


class GlobalIssueCountAccuracyTests(TestCase):
    def setUp(self):
        self.client = Client(SERVER_NAME="localhost")
        AnalyzedRepository.objects.filter(owner__in=["accuracy-test", "django-mock", "sentry-mock"]).delete()

    def tearDown(self):
        AnalyzedRepository.objects.filter(owner__in=["accuracy-test", "django-mock", "sentry-mock"]).delete()

    # 1. Repository with GitHub Issues enabled and multiple open issues
    @patch("core.github_service._fetch_collection_count")
    @patch("core.github_service._make_github_request")
    def test_enabled_issues_multiple_open_issues(self, mock_request, mock_count):
        mock_request.side_effect = lambda url, return_headers=False: (
            {"total_count": 42} if "search/issues" in url else (
                {"Python": 1000} if "languages" in url else (
                    {"files": {}} if "community/profile" in url else {
                        "description": "Repo with 42 issues",
                        "language": "Python",
                        "stargazers_count": 500,
                        "forks_count": 100,
                        "has_issues": True,
                        "open_issues_count": 60,  # 42 issues + 18 PRs
                        "pushed_at": "2026-10-12T00:00:00Z",
                    }
                )
            )
        )
        mock_count.side_effect = lambda url: 18 if "pulls" in url else 0

        meta = fetch_github_metadata("accuracy-test", "issues-repo")
        self.assertTrue(meta["has_issues"])
        self.assertEqual(meta["open_issues_count"], 42)
        self.assertEqual(meta["open_prs_count"], 18)
        self.assertIn("is%3Aissue+is%3Aopen", meta["issue_tracker_url"])

    # 2. Open issues and open PRs, ensuring PRs are strictly excluded
    @patch("core.github_service._fetch_collection_count")
    @patch("core.github_service._make_github_request")
    def test_open_issues_strictly_excludes_prs(self, mock_request, mock_count):
        mock_request.side_effect = lambda url, return_headers=False: (
            {"total_count": 75} if "search/issues" in url else (
                {"Python": 1000} if "languages" in url else (
                    {"files": {}} if "community/profile" in url else {
                        "description": "Repo testing PR exclusion",
                        "language": "Python",
                        "stargazers_count": 300,
                        "forks_count": 50,
                        "has_issues": True,
                        "open_issues_count": 100,  # 75 issues + 25 PRs
                        "pushed_at": "2026-10-12T00:00:00Z",
                    }
                )
            )
        )
        mock_count.side_effect = lambda url: 25 if "pulls" in url else 0

        meta = fetch_github_metadata("accuracy-test", "pr-exclusion-repo")
        self.assertEqual(meta["open_issues_count"], 75)
        self.assertEqual(meta["open_prs_count"], 25)

    # 3. Repository with zero open issues
    @patch("core.github_service._fetch_collection_count")
    @patch("core.github_service._make_github_request")
    def test_zero_open_issues_returns_zero_integer(self, mock_request, mock_count):
        mock_request.side_effect = lambda url, return_headers=False: (
            {"total_count": 0} if "search/issues" in url else (
                {"Python": 1000} if "languages" in url else (
                    {"files": {}} if "community/profile" in url else {
                        "description": "Zero issues repo",
                        "language": "Python",
                        "stargazers_count": 100,
                        "forks_count": 10,
                        "has_issues": True,
                        "open_issues_count": 5,  # 0 issues + 5 PRs
                        "pushed_at": "2026-10-12T00:00:00Z",
                    }
                )
            )
        )
        mock_count.side_effect = lambda url: 5 if "pulls" in url else 0

        meta = fetch_github_metadata("accuracy-test", "zero-issues-repo")
        self.assertTrue(meta["has_issues"])
        self.assertEqual(meta["open_issues_count"], 0)
        self.assertIsNotNone(meta["open_issues_count"])

    # 4. Repository with GitHub Issues disabled
    @patch("core.github_service._fetch_collection_count")
    @patch("core.github_service._make_github_request")
    def test_disabled_issues_returns_none(self, mock_request, mock_count):
        mock_request.side_effect = lambda url, return_headers=False: (
            {"Python": 1000} if "languages" in url else (
                {"files": {}} if "community/profile" in url else {
                    "description": "Disabled issues repo",
                    "language": "Python",
                    "stargazers_count": 1000,
                    "forks_count": 200,
                    "has_issues": False,
                    "open_issues_count": 500,  # 500 PRs in GitHub metadata
                    "pushed_at": "2026-10-12T00:00:00Z",
                }
            )
        )
        mock_count.side_effect = lambda url: 490 if "pulls" in url else 0

        meta = fetch_github_metadata("accuracy-test", "disabled-issues-repo")
        self.assertFalse(meta["has_issues"])
        self.assertIsNone(meta["open_issues_count"])

    # 5. Repository with verified external tracker
    @patch("core.github_service._fetch_collection_count")
    @patch("core.github_service._make_github_request")
    def test_disabled_issues_with_verified_external_tracker(self, mock_request, mock_count):
        mock_request.side_effect = lambda url, return_headers=False: (
            {"Python": 1000} if "languages" in url else (
                {"files": {}} if "community/profile" in url else {
                    "description": "Bugs tracked in Jira: https://issues.apache.org/jira/browse/KAFKA",
                    "language": "Java",
                    "stargazers_count": 25000,
                    "forks_count": 12000,
                    "has_issues": False,
                    "open_issues_count": 300,
                    "pushed_at": "2026-10-12T00:00:00Z",
                }
            )
        )
        mock_count.side_effect = lambda url: 300 if "pulls" in url else 0

        meta = fetch_github_metadata("accuracy-test", "kafka-test")
        self.assertFalse(meta["has_issues"])
        self.assertIsNone(meta["open_issues_count"])
        self.assertEqual(meta["issue_tracker_url"], "https://issues.apache.org/jira/browse/KAFKA")

    # 6. Repository without verifiable external tracker
    @patch("core.github_service._fetch_collection_count")
    @patch("core.github_service._make_github_request")
    def test_disabled_issues_without_verifiable_external_tracker(self, mock_request, mock_count):
        mock_request.side_effect = lambda url, return_headers=False: (
            {"Python": 1000} if "languages" in url else (
                {"files": {}} if "community/profile" in url else {
                    "description": "No tracker mentioned here. Read our blog at https://example.com/known-issues",
                    "language": "Python",
                    "stargazers_count": 500,
                    "forks_count": 50,
                    "has_issues": False,
                    "open_issues_count": 40,
                    "pushed_at": "2026-10-12T00:00:00Z",
                }
            )
        )
        mock_count.side_effect = lambda url: 40 if "pulls" in url else 0

        meta = fetch_github_metadata("accuracy-test", "no-tracker-repo")
        self.assertFalse(meta["has_issues"])
        self.assertIsNone(meta["open_issues_count"])
        self.assertIsNone(meta["issue_tracker_url"])

    # 7. Metadata count differs from Search API issue-only count
    @patch("core.github_service._fetch_collection_count")
    @patch("core.github_service._make_github_request")
    def test_search_count_preferred_over_desynchronized_metadata_count(self, mock_request, mock_count):
        mock_request.side_effect = lambda url, return_headers=False: (
            {"total_count": 1737} if "search/issues" in url else (
                {"Python": 1000} if "languages" in url else (
                    {"files": {}} if "community/profile" in url else {
                        "description": "Sentry mock",
                        "language": "Python",
                        "stargazers_count": 40000,
                        "forks_count": 4000,
                        "has_issues": True,
                        "open_issues_count": 2296,
                        "pushed_at": "2026-10-12T00:00:00Z",
                    }
                )
            )
        )
        mock_count.side_effect = lambda url: 549 if "pulls" in url else 0

        meta = fetch_github_metadata("accuracy-test", "desync-repo")
        self.assertEqual(meta["open_issues_count"], 1737)

    # 8. Stale database count corrected by successful reanalysis
    @patch("core.github_service._fetch_collection_count")
    @patch("core.github_service._make_github_request")
    def test_stale_count_corrected_by_reanalysis(self, mock_request, mock_count):
        repo = AnalyzedRepository.objects.create(
            owner="accuracy-test",
            name="stale-repo",
            url="https://github.com/accuracy-test/stale-repo",
            has_issues=True,
            open_issues_count=6,
            open_prs_count=500,
            stars_count=1000,
        )

        mock_request.side_effect = lambda url, return_headers=False: (
            {"Python": 1000} if "languages" in url else (
                {"files": {}} if "community/profile" in url else {
                    "description": "Updated to disabled issues",
                    "homepage": "https://code.djangoproject.com/newticket",
                    "language": "Python",
                    "stargazers_count": 1000,
                    "forks_count": 100,
                    "has_issues": False,
                    "open_issues_count": 535,
                    "pushed_at": "2026-10-12T00:00:00Z",
                }
            )
        )
        mock_count.side_effect = lambda url: 535 if "pulls" in url else 0

        updated = analyze_and_save_repository("https://github.com/accuracy-test/stale-repo")
        self.assertEqual(updated.id, repo.id)
        self.assertFalse(updated.has_issues)
        self.assertIsNone(updated.open_issues_count)
        self.assertEqual(updated.issue_tracker_url, "https://code.djangoproject.com/newticket")

    # 9. Search API failure without prior record yields None (Unavailable) without subtracting PRs
    @patch("core.github_service._fetch_collection_count")
    @patch("core.github_service._make_github_request")
    def test_search_api_failure_without_prior_record_yields_unavailable(self, mock_request, mock_count):
        def req_side_effect(url, return_headers=False):
            if "search/issues" in url:
                raise RateLimitError("Search API rate limit exceeded")
            elif "languages" in url:
                return {"Python": 1000}
            elif "community/profile" in url:
                return {"files": {}}
            return {
                "description": "Fallback repo",
                "language": "Python",
                "stargazers_count": 200,
                "forks_count": 30,
                "has_issues": True,
                "open_issues_count": 50,
                "pushed_at": "2026-10-12T00:00:00Z",
            }

        mock_request.side_effect = req_side_effect
        mock_count.side_effect = lambda url: 15 if "pulls" in url else 0

        # fetch_github_metadata must NOT subtract PRs: open_issues_count must be None
        meta = fetch_github_metadata("accuracy-test", "fallback-repo")
        self.assertTrue(meta["has_issues"])
        self.assertIsNone(meta["open_issues_count"])
        self.assertEqual(meta["open_prs_count"], 15)

        # analyze_and_save_repository on first analysis saves None and is NOT stale
        saved = analyze_and_save_repository("https://github.com/accuracy-test/fallback-repo")
        self.assertIsNone(saved.open_issues_count)
        self.assertFalse(saved.issues_count_is_stale)
        self.assertEqual(saved.display_issues_count, "Unavailable")
        self.assertEqual(saved.issues_label, "Open Issues (Unavailable)")

    # 9a. Search API failure during refresh preserves last-known count and marks stale
    @patch("core.github_service._fetch_collection_count")
    @patch("core.github_service._make_github_request")
    def test_search_api_failure_on_refresh_preserves_count_and_marks_stale(self, mock_request, mock_count):
        repo = AnalyzedRepository.objects.create(
            owner="accuracy-test",
            name="stale-refresh-repo",
            url="https://github.com/accuracy-test/stale-refresh-repo",
            has_issues=True,
            open_issues_count=1737,
            open_prs_count=43,
            stars_count=10000,
            issues_count_is_stale=False,
        )

        def req_side_effect(url, return_headers=False):
            if "search/issues" in url:
                raise RateLimitError("Search API secondary rate limit exceeded")
            elif "languages" in url:
                return {"Python": 5000}
            elif "community/profile" in url:
                return {"files": {}}
            return {
                "description": "Repo refreshed during Search API rate limit",
                "language": "Python",
                "stargazers_count": 10500,
                "forks_count": 1000,
                "has_issues": True,
                "open_issues_count": 2200,  # Conflated/cached counter
                "pushed_at": "2026-10-12T00:00:00Z",
            }

        mock_request.side_effect = req_side_effect
        mock_count.side_effect = lambda url: 45 if "pulls" in url else 0

        updated = analyze_and_save_repository("https://github.com/accuracy-test/stale-refresh-repo")
        self.assertEqual(updated.id, repo.id)
        # Must preserve the exact last-known verified count (1737), NOT 0, NOT None, NOT 2200-45
        self.assertEqual(updated.open_issues_count, 1737)
        self.assertTrue(updated.issues_count_is_stale)
        self.assertEqual(updated.display_issues_count, "1737 (Stale)")
        self.assertEqual(updated.issues_label, "Open Issues (Stale)")

    # 9b. Subsequent successful refresh updates count and clears stale flag
    @patch("core.github_service._fetch_collection_count")
    @patch("core.github_service._make_github_request")
    def test_subsequent_successful_refresh_clears_stale_flag(self, mock_request, mock_count):
        repo = AnalyzedRepository.objects.create(
            owner="accuracy-test",
            name="clear-stale-repo",
            url="https://github.com/accuracy-test/clear-stale-repo",
            has_issues=True,
            open_issues_count=1737,
            open_prs_count=43,
            stars_count=10000,
            issues_count_is_stale=True,
        )

        def req_side_effect(url, return_headers=False):
            if "search/issues" in url:
                return {"total_count": 1740}
            elif "languages" in url:
                return {"Python": 5000}
            elif "community/profile" in url:
                return {"files": {}}
            return {
                "description": "Repo refreshed successfully",
                "language": "Python",
                "stargazers_count": 10500,
                "forks_count": 1000,
                "has_issues": True,
                "open_issues_count": 2250,
                "pushed_at": "2026-10-12T00:00:00Z",
            }

        mock_request.side_effect = req_side_effect
        mock_count.side_effect = lambda url: 45 if "pulls" in url else 0

        updated = analyze_and_save_repository("https://github.com/accuracy-test/clear-stale-repo")
        self.assertEqual(updated.id, repo.id)
        self.assertEqual(updated.open_issues_count, 1740)
        self.assertFalse(updated.issues_count_is_stale)
        self.assertEqual(updated.display_issues_count, "1740")
        self.assertEqual(updated.issues_label, "Open Issues")

    # 9c. Failed refresh does not silently replace valid saved values with zero
    @patch("core.github_service._fetch_collection_count")
    @patch("core.github_service._make_github_request")
    def test_failed_refresh_does_not_replace_saved_count_with_zero(self, mock_request, mock_count):
        repo = AnalyzedRepository.objects.create(
            owner="accuracy-test",
            name="nonzero-repo",
            url="https://github.com/accuracy-test/nonzero-repo",
            has_issues=True,
            open_issues_count=88,
            open_prs_count=5,
            stars_count=500,
            issues_count_is_stale=False,
        )

        def req_side_effect(url, return_headers=False):
            if "search/issues" in url:
                raise NetworkError("Handshake timed out")
            elif "languages" in url:
                return {"Python": 1000}
            elif "community/profile" in url:
                return {"files": {}}
            return {
                "description": "Failed search API refresh",
                "language": "Python",
                "stargazers_count": 550,
                "forks_count": 60,
                "has_issues": True,
                "open_issues_count": 93,
                "pushed_at": "2026-10-12T00:00:00Z",
            }

        mock_request.side_effect = req_side_effect
        mock_count.side_effect = lambda url: 5 if "pulls" in url else 0

        updated = analyze_and_save_repository("https://github.com/accuracy-test/nonzero-repo")
        self.assertNotEqual(updated.open_issues_count, 0)
        self.assertEqual(updated.open_issues_count, 88)
        self.assertTrue(updated.issues_count_is_stale)

    # 9d. Failed refresh preserves existing database record without wiping or zeroing on total error
    @patch("core.github_service.fetch_github_metadata")
    def test_failed_refresh_preserves_existing_database_record(self, mock_fetch):
        repo = AnalyzedRepository.objects.create(
            owner="accuracy-test",
            name="persist-repo",
            url="https://github.com/accuracy-test/persist-repo",
            has_issues=True,
            open_issues_count=42,
            open_prs_count=10,
            stars_count=500,
        )

        mock_fetch.side_effect = NetworkError("Connection timed out during refresh")

        with self.assertRaises(NetworkError):
            analyze_and_save_repository("https://github.com/accuracy-test/persist-repo")

        repo.refresh_from_db()
        self.assertEqual(repo.open_issues_count, 42)
        self.assertTrue(repo.has_issues)

    # 10. Unanalysed repository in OSS Navigator
    @patch("core.views.search_github_repositories")
    def test_unanalysed_repo_in_navigator_displays_unanalyzed(self, mock_search):
        mock_search.return_value = {
            "total_count": 1,
            "items": [
                {
                    "full_name": "unknown-org/fresh-project",
                    "name": "fresh-project",
                    "owner": {"login": "unknown-org"},
                    "description": "A repo never analyzed in RepoPilot",
                    "html_url": "https://github.com/unknown-org/fresh-project",
                    "stargazers_count": 100,
                    "forks_count": 20,
                    "open_issues_count": 85,
                    "has_issues": True,
                    "language": "Go",
                    "topics": [],
                }
            ],
            "page": 1,
            "per_page": 12,
            "total_pages": 1,
            "has_next": False,
            "has_previous": False,
            "query_used": "test",
        }

        response = self.client.get(reverse("navigator"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "fresh-project")
        self.assertContains(response, "Unanalyzed")

    # 11. Consistent metrics across Repository Analysis, OSS Navigator, and OSS Mentor
    @patch("core.views.fetch_repository_documentation")
    @patch("core.views.search_github_repositories")
    def test_consistent_metrics_across_all_three_modules(self, mock_search, mock_doc):
        repo = AnalyzedRepository.objects.create(
            owner="accuracy-test",
            name="consistent-repo",
            url="https://github.com/accuracy-test/consistent-repo",
            has_issues=False,
            open_issues_count=None,
            issue_tracker_url="https://code.djangoproject.com/newticket",
            stars_count=2000,
            forks_count=300,
            readiness_score=90.0,
        )

        mock_doc.return_value = {
            "has_readme": True,
            "readme_excerpt": "Consistent repo docs",
            "issue_tracker_url": "https://code.djangoproject.com/newticket",
            "contributing_url": None,
            "code_of_conduct_url": None,
            "license_url": None,
            "security_url": None,
        }

        mock_search.return_value = {
            "total_count": 1,
            "items": [
                {
                    "full_name": "accuracy-test/consistent-repo",
                    "name": "consistent-repo",
                    "owner": {"login": "accuracy-test"},
                    "description": "A repo testing consistency",
                    "html_url": "https://github.com/accuracy-test/consistent-repo",
                    "stargazers_count": 2000,
                    "forks_count": 300,
                    "has_issues": False,
                    "open_issues_count": 500,
                    "language": "Python",
                    "topics": [],
                }
            ],
            "page": 1,
            "per_page": 12,
            "total_pages": 1,
            "has_next": False,
            "has_previous": False,
            "query_used": "test",
        }

        # 1. Home / Repository Analysis
        with patch("core.views.analyze_and_save_repository", return_value=repo):
            resp_home_post = self.client.post(reverse("home"), {"repo_url": repo.url})
            self.assertContains(resp_home_post, "Issues (External/N/A)")
            self.assertContains(resp_home_post, "N/A")

        # 2. OSS Navigator
        resp_nav = self.client.get(reverse("navigator"))
        self.assertEqual(resp_nav.status_code, 200)
        self.assertContains(resp_nav, "N/A")
        self.assertContains(resp_nav, "Readiness: 90/100")

        # 3. OSS Mentor
        resp_mentor = self.client.get(reverse("mentor"), {"repo": "accuracy-test/consistent-repo"})
        self.assertEqual(resp_mentor.status_code, 200)
        self.assertContains(resp_mentor, "Issues (External/N/A)")
        self.assertContains(resp_mentor, "N/A")
        self.assertContains(resp_mentor, "https://code.djangoproject.com/newticket")

    # 12. Regression test: django/django
    @patch("core.github_service._fetch_collection_count")
    @patch("core.github_service._make_github_request")
    def test_regression_django_scenario(self, mock_request, mock_count):
        mock_request.side_effect = lambda url, return_headers=False: (
            {"Python": 500000} if "languages" in url else (
                {"files": {}} if "community/profile" in url else {
                    "description": "The Web framework for perfectionists. Tracker: https://code.djangoproject.com/newticket",
                    "homepage": "https://www.djangoproject.com/",
                    "language": "Python",
                    "stargazers_count": 80000,
                    "forks_count": 32000,
                    "has_issues": False,
                    "open_issues_count": 541,
                    "pushed_at": "2026-10-12T00:00:00Z",
                    "license": {"name": "BSD-3-Clause"},
                }
            )
        )
        mock_count.side_effect = lambda url: 535 if "pulls" in url else 10

        meta = fetch_github_metadata("django-mock", "django")
        self.assertFalse(meta["has_issues"])
        self.assertIsNone(meta["open_issues_count"])
        self.assertEqual(meta["open_prs_count"], 535)
        self.assertEqual(meta["issue_tracker_url"], "https://code.djangoproject.com/newticket")

    # 12b. Regression test: getsentry/sentry
    @patch("core.github_service._fetch_collection_count")
    @patch("core.github_service._make_github_request")
    def test_regression_sentry_scenario(self, mock_request, mock_count):
        mock_request.side_effect = lambda url, return_headers=False: (
            {"total_count": 1737} if "search/issues" in url else (
                {"Python": 800000} if "languages" in url else (
                    {"files": {}} if "community/profile" in url else {
                        "description": "Developer-first error tracking and performance monitoring",
                        "homepage": "https://sentry.io",
                        "language": "Python",
                        "stargazers_count": 40000,
                        "forks_count": 4200,
                        "has_issues": True,
                        "open_issues_count": 2296,
                        "pushed_at": "2026-10-12T00:00:00Z",
                        "license": {"name": "FSL-1.1-Apache-2.0"},
                    }
                )
            )
        )
        mock_count.side_effect = lambda url: 549 if "pulls" in url else 50

        meta = fetch_github_metadata("sentry-mock", "sentry")
        self.assertTrue(meta["has_issues"])
        self.assertEqual(meta["open_issues_count"], 1737)
        self.assertEqual(meta["open_prs_count"], 549)
        self.assertEqual(meta["issue_tracker_url"], "https://github.com/sentry-mock/sentry/issues?q=is%3Aissue+is%3Aopen")

    # Edge case: missing JSON fields
    @patch("core.github_service._fetch_collection_count")
    @patch("core.github_service._make_github_request")
    def test_missing_json_fields_handled_gracefully(self, mock_request, mock_count):
        mock_request.side_effect = lambda url, return_headers=False: (
            {} if "search/issues" in url else (
                {} if "languages" in url else (
                    {"files": {}} if "community/profile" in url else {
                        "stargazers_count": 10,
                        "forks_count": 2,
                    }
                )
            )
        )
        mock_count.side_effect = lambda url: 0

        meta = fetch_github_metadata("accuracy-test", "sparse-repo")
        self.assertTrue(meta["has_issues"])
        self.assertIsNone(meta["open_issues_count"])
        self.assertEqual(meta["open_prs_count"], 0)

    # 13. UI Tests: Stale issue badge rendered across all modules
    @patch("core.views.fetch_repository_documentation")
    @patch("core.views.search_github_repositories")
    def test_stale_issue_badge_rendered_across_all_modules(self, mock_search, mock_doc):
        repo = AnalyzedRepository.objects.create(
            owner="accuracy-test",
            name="stale-ui-repo",
            url="https://github.com/accuracy-test/stale-ui-repo",
            has_issues=True,
            open_issues_count=1737,
            issues_count_is_stale=True,
            stars_count=2000,
            forks_count=300,
            readiness_score=90.0,
        )

        mock_doc.return_value = {
            "has_readme": True,
            "readme_excerpt": "Stale repo docs",
            "issue_tracker_url": None,
            "contributing_url": None,
            "code_of_conduct_url": None,
            "license_url": None,
            "security_url": None,
        }

        mock_search.return_value = {
            "total_count": 1,
            "items": [
                {
                    "full_name": "accuracy-test/stale-ui-repo",
                    "name": "stale-ui-repo",
                    "owner": {"login": "accuracy-test"},
                    "description": "A stale repo",
                    "html_url": "https://github.com/accuracy-test/stale-ui-repo",
                    "stargazers_count": 2000,
                    "forks_count": 300,
                    "has_issues": True,
                    "open_issues_count": 1737,
                    "language": "Python",
                    "topics": [],
                }
            ],
            "page": 1,
            "per_page": 12,
            "total_pages": 1,
            "has_next": False,
            "has_previous": False,
            "query_used": "test",
        }

        # 1. Home / Repository Analysis
        with patch("core.views.analyze_and_save_repository", return_value=repo):
            resp_home = self.client.post(reverse("home"), {"repo_url": repo.url})
            self.assertContains(resp_home, "Open Issues (Stale)")
            self.assertContains(resp_home, "1737 (Stale)")

        # 2. OSS Navigator
        resp_nav = self.client.get(reverse("navigator"))
        self.assertEqual(resp_nav.status_code, 200)
        self.assertContains(resp_nav, "1737 (Stale)")

        # 3. OSS Mentor
        resp_mentor = self.client.get(reverse("mentor"), {"repo": "accuracy-test/stale-ui-repo"})
        self.assertEqual(resp_mentor.status_code, 200)
        self.assertContains(resp_mentor, "Open Issues (Stale)")
        self.assertContains(resp_mentor, "Search API Notice")

    # 14. UI Tests: Unavailable issue badge rendered across all modules
    @patch("core.views.fetch_repository_documentation")
    @patch("core.views.search_github_repositories")
    def test_unavailable_issue_badge_rendered_across_all_modules(self, mock_search, mock_doc):
        repo = AnalyzedRepository.objects.create(
            owner="accuracy-test",
            name="unavailable-ui-repo",
            url="https://github.com/accuracy-test/unavailable-ui-repo",
            has_issues=True,
            open_issues_count=None,
            issues_count_is_stale=False,
            stars_count=1500,
            forks_count=150,
            readiness_score=85.0,
        )

        mock_doc.return_value = {
            "has_readme": True,
            "readme_excerpt": "Unavailable repo docs",
            "issue_tracker_url": None,
            "contributing_url": None,
            "code_of_conduct_url": None,
            "license_url": None,
            "security_url": None,
        }

        mock_search.return_value = {
            "total_count": 1,
            "items": [
                {
                    "full_name": "accuracy-test/unavailable-ui-repo",
                    "name": "unavailable-ui-repo",
                    "owner": {"login": "accuracy-test"},
                    "description": "An unavailable repo",
                    "html_url": "https://github.com/accuracy-test/unavailable-ui-repo",
                    "stargazers_count": 1500,
                    "forks_count": 150,
                    "has_issues": True,
                    "open_issues_count": None,
                    "language": "Go",
                    "topics": [],
                }
            ],
            "page": 1,
            "per_page": 12,
            "total_pages": 1,
            "has_next": False,
            "has_previous": False,
            "query_used": "test",
        }

        # 1. Home / Repository Analysis
        with patch("core.views.analyze_and_save_repository", return_value=repo):
            resp_home = self.client.post(reverse("home"), {"repo_url": repo.url})
            self.assertContains(resp_home, "Open Issues (Unavailable)")
            self.assertContains(resp_home, "Unavailable")

        # 2. OSS Navigator
        resp_nav = self.client.get(reverse("navigator"))
        self.assertEqual(resp_nav.status_code, 200)
        self.assertContains(resp_nav, "Unavailable")

        # 3. OSS Mentor
        resp_mentor = self.client.get(reverse("mentor"), {"repo": "accuracy-test/unavailable-ui-repo"})
        self.assertEqual(resp_mentor.status_code, 200)
        self.assertContains(resp_mentor, "Open Issues (Unavailable)")
        self.assertContains(resp_mentor, "Issue Count Notice")


class IssueServiceTests(TestCase):
    """
    Unit and integration tests for Issue2Contribution service (core/issue_service.py).
    All GitHub API requests are mocked; zero network calls are made.
    """

    def test_parse_valid_issue_urls(self):
        cases = [
            ("https://github.com/django/django/issues/12345", ("django", "django", 12345)),
            ("http://github.com/django/django/issues/12345/", ("django", "django", 12345)),
            ("github.com/pallets/flask/issues/5000", ("pallets", "flask", 5000)),
            ("https://github.com/psf/black/issues/42#issuecomment-9999", ("psf", "black", 42)),
            ("https://www.github.com/facebook/react/issues/100", ("facebook", "react", 100)),
            ("pallets/flask#5000", ("pallets", "flask", 5000)),
            ("django/django#1", ("django", "django", 1)),
        ]
        for url_str, expected in cases:
            with self.subTest(url=url_str):
                self.assertEqual(parse_github_issue_url(url_str), expected)

    def test_parse_invalid_issue_urls(self):
        invalid_cases = [
            "",
            "   ",
            None,
            "https://gitlab.com/django/django/issues/1",
            "https://github.com/django/django",  # missing issue number
            "https://github.com/django/django/issues",  # missing number
            "https://github.com/django/django/issues/abc",  # non-numeric
            "https://github.com/django/django/issues/-5",  # negative
            "https://github.com/django/django/issues/0",  # zero
            "django/django#0",  # zero shorthand
            "django/django#-10",  # negative shorthand
            "invalid$owner/django#1",  # invalid chars
        ]
        for case in invalid_cases:
            with self.subTest(case=case):
                with self.assertRaises(InvalidURLError):
                    parse_github_issue_url(case)

    def test_parse_pull_request_url_raises_pr_error(self):
        pr_urls = [
            "https://github.com/django/django/pull/12345",
            "http://github.com/pallets/flask/pulls/100",
            "github.com/facebook/react/pull/500",
        ]
        for pr_url in pr_urls:
            with self.subTest(url=pr_url):
                with self.assertRaises(PullRequestNotSupportedError) as cm:
                    parse_github_issue_url(pr_url)
                self.assertIn("Pull Request", str(cm.exception))

    @patch("core.issue_service._make_github_request")
    def test_fetch_github_issue_success(self, mock_request):
        mock_request.return_value = {
            "title": "Fix crash in URL resolver when reverse matching",
            "body": "When reversing URLs with invalid args, an unhandled exception is thrown.\nSee `django/urls/resolvers.py` line 45.",
            "state": "open",
            "html_url": "https://github.com/django/django/issues/12345",
            "user": {"login": "contributor1", "avatar_url": "https://avatars.githubusercontent.com/u/1"},
            "labels": [
                {"name": "bug", "color": "d73a4a", "description": "Something isn't working"},
                {"name": "good first issue", "color": "7057ff", "description": "Good for newcomers"},
            ],
            "comments": 4,
            "created_at": "2026-10-10T12:00:00Z",
            "updated_at": "2026-10-11T15:30:00Z",
            "closed_at": None,
        }

        issue = fetch_github_issue("django", "django", 12345)
        self.assertEqual(issue["owner"], "django")
        self.assertEqual(issue["name"], "django")
        self.assertEqual(issue["issue_number"], 12345)
        self.assertEqual(issue["title"], "Fix crash in URL resolver when reverse matching")
        self.assertEqual(issue["state"], "open")
        self.assertEqual(issue["author"], "contributor1")
        self.assertEqual(issue["comments_count"], 4)
        self.assertEqual(len(issue["labels"]), 2)
        self.assertFalse(issue["is_pull_request"])
        mock_request.assert_called_once_with("https://api.github.com/repos/django/django/issues/12345")

    @patch("core.issue_service._make_github_request")
    def test_fetch_github_issue_detects_pull_request_payload(self, mock_request):
        mock_request.return_value = {
            "title": "PR: Refactor auth middleware",
            "body": "This PR refactors middleware.",
            "state": "open",
            "pull_request": {"url": "https://api.github.com/repos/django/django/pulls/500"},
            "user": {"login": "pr_author"},
            "labels": [],
            "comments": 1,
        }

        issue = fetch_github_issue("django", "django", 500)
        self.assertTrue(issue["is_pull_request"])

    @patch("core.issue_service._make_github_request")
    def test_fetch_github_issue_not_found(self, mock_request):
        mock_request.return_value = None
        with self.assertRaises(IssueNotFoundError):
            fetch_github_issue("django", "django", 999999)

    @patch("core.issue_service._make_github_request")
    def test_fetch_github_issue_rate_limit_and_network_errors(self, mock_request):
        mock_request.side_effect = RateLimitError("Rate limit exceeded")
        with self.assertRaises(RateLimitError):
            fetch_github_issue("django", "django", 10)

        mock_request.side_effect = NetworkError("Connection timed out")
        with self.assertRaises(NetworkError):
            fetch_github_issue("django", "django", 10)

    def test_extract_issue_evidence_explicit_files_and_symbols(self):
        title = "TypeError when calling resolve_model in models.py"
        body = """
        I encountered this error when running migrations:
        ```python
        File "django/db/models/base.py", line 420, in resolve_model
            return getattr(cls, name)
        ```
        Also see `django/core/checks/model_checks.py`.
        Calling `ModelBase.__new__()` triggers `TypeError: NoneType is not callable`.
        """
        labels = [{"name": "bug"}, {"name": "component:ORM"}]

        evidence = extract_issue_evidence(title, body, labels)
        self.assertTrue(evidence["has_files"])
        self.assertIn("django/db/models/base.py", evidence["files"])
        self.assertIn("django/core/checks/model_checks.py", evidence["files"])
        self.assertIn("models.py", evidence["files"])
        self.assertTrue(evidence["is_bug"])
        self.assertFalse(evidence["is_doc"])
        self.assertTrue(len(evidence["errors"]) > 0)
        self.assertTrue(len(evidence["symbols"]) > 0)

    def test_extract_issue_evidence_zero_files_never_hallucinates(self):
        title = "Improve documentation for beginners"
        body = "The tutorial needs clearer steps for Windows setup."
        labels = [{"name": "documentation"}, {"name": "good first issue"}]

        evidence = extract_issue_evidence(title, body, labels)
        self.assertFalse(evidence["has_files"])
        self.assertEqual(evidence["files"], [])
        self.assertTrue(evidence["is_doc"])
        self.assertTrue(evidence["is_good_first_issue"])
        self.assertFalse(evidence["is_bug"])

    def test_generate_contribution_plan_structure(self):
        issue_data = {
            "owner": "pallets",
            "name": "flask",
            "issue_number": 5000,
            "title": "Bug in blueprint url_prefix handling",
            "body": "Stack trace:\nFile \"src/flask/blueprints.py\", line 110, in register\nTypeError: url_prefix must be str",
            "state": "open",
            "labels": [{"name": "bug"}, {"name": "good first issue"}],
        }

        plan = generate_contribution_plan(issue_data)
        # Verify 6 parts
        self.assertIn("objective", plan)
        self.assertIn("investigation", plan)
        self.assertIn("evidence", plan)
        self.assertIn("implementation", plan)
        self.assertIn("testing", plan)
        self.assertIn("pull_request", plan)

        # Check part contents
        self.assertEqual(plan["objective"]["category"], "Bug Fix")
        self.assertTrue(plan["objective"]["is_good_first_issue"])
        self.assertTrue(plan["evidence"]["has_files"])
        self.assertIn("src/flask/blueprints.py", plan["evidence"]["files"])
        self.assertEqual(plan["pull_request"]["closing_keyword"], "Fixes #5000")

    def test_generate_contribution_plan_zero_evidence_notice(self):
        issue_data = {
            "owner": "pallets",
            "name": "flask",
            "issue_number": 5001,
            "title": "Navbar dropdown glitch on Safari",
            "body": "Clicking the profile icon does nothing on mobile Safari.",
            "state": "open",
            "labels": [{"name": "bug"}],
        }

        plan = generate_contribution_plan(issue_data)
        self.assertFalse(plan["evidence"]["has_files"])
        self.assertIn("No specific file paths or source files were cited", plan["evidence"]["notice"])

    @patch("core.issue_service.fetch_github_issue")
    def test_analyze_issue_and_generate_plan_orchestrator(self, mock_fetch):
        mock_fetch.return_value = {
            "owner": "pallets",
            "name": "flask",
            "issue_number": 42,
            "title": "Doc update",
            "body": "Fix typo in `quickstart.rst`.",
            "state": "open",
            "labels": [{"name": "documentation"}],
            "is_pull_request": False,
        }

        res = analyze_issue_and_generate_plan("pallets/flask#42")
        self.assertEqual(res["owner"], "pallets")
        self.assertEqual(res["name"], "flask")
        self.assertEqual(res["issue_number"], 42)
        self.assertIsNotNone(res["plan"])
        self.assertEqual(res["plan"]["objective"]["category"], "Documentation Improvement")

    @patch("core.issue_service.fetch_github_issue")
    def test_analyze_issue_and_generate_plan_pr_returns_none_plan(self, mock_fetch):
        mock_fetch.return_value = {
            "owner": "pallets",
            "name": "flask",
            "issue_number": 99,
            "title": "PR: Refactor core",
            "body": "PR description",
            "state": "open",
            "labels": [],
            "is_pull_request": True,
        }

        res = analyze_issue_and_generate_plan("pallets/flask#99")
        self.assertTrue(res["issue"]["is_pull_request"])
        self.assertIsNone(res["plan"])

    def test_parse_unsupported_url_schemes_rejected(self):
        unsupported_schemes = [
            "ftp://github.com/django/django/issues/123",
            "ssh://github.com/django/django/issues/123",
            "git://github.com/django/django/issues/123",
            "file:///github.com/django/django/issues/123",
        ]
        for url in unsupported_schemes:
            with self.subTest(url=url):
                with self.assertRaises(InvalidURLError) as cm:
                    parse_github_issue_url(url)
                self.assertIn("scheme", str(cm.exception).lower())

    def test_extract_evidence_javascript_node_stack_trace(self):
        title = "Uncaught TypeError in production bundle"
        body = """
        Error occurred during initialization:
        TypeError: Cannot read properties of undefined (reading 'mount')
            at Object.<anonymous> (/app/src/components/App.tsx:42:15)
            at initRouter (src/router/index.ts:108:5)
        """
        evidence = extract_issue_evidence(title, body, [])
        self.assertIn("/app/src/components/App.tsx", evidence["files"])
        self.assertIn("src/router/index.ts", evidence["files"])
        self.assertTrue(len(evidence["errors"]) > 0)
        self.assertIn("TypeError", evidence["errors"][0])

    def test_extract_evidence_unified_diff_headers(self):
        title = "Proposed patch for logger"
        body = """
        Here is the diff:
        --- a/src/logger/old.py
        +++ b/src/logger/handler.py
        @@ -10,3 +10,4 @@
        """
        evidence = extract_issue_evidence(title, body, [])
        self.assertIn("src/logger/handler.py", evidence["files"])
        self.assertIn("src/logger/old.py", evidence["files"])
        self.assertNotIn("b/src/logger/handler.py", evidence["files"])

    def test_extract_evidence_windows_style_paths(self):
        title = "Crash on Windows"
        body = r"Exception thrown in src\flask\blueprints.py line 45 and config\settings.py."
        evidence = extract_issue_evidence(title, body, [])
        self.assertIn("src/flask/blueprints.py", evidence["files"])
        self.assertIn("config/settings.py", evidence["files"])

    def test_extract_evidence_filters_attachments_including_png(self):
        title = "UI bug with screenshots"
        body = """
        See attached screenshot: `diagram.png` and `flowchart.jpg`.
        Also uploaded archive.zip and document.pdf.
        Actual bug in `views.py`.
        """
        evidence = extract_issue_evidence(title, body, [])
        self.assertIn("views.py", evidence["files"])
        self.assertNotIn("diagram.png", evidence["files"])
        self.assertNotIn("flowchart.jpg", evidence["files"])
        self.assertNotIn("archive.zip", evidence["files"])
        self.assertNotIn("document.pdf", evidence["files"])

    def test_extract_evidence_filters_version_numbers_including_v_prefix(self):
        title = "Bug occurs on Python 3.10.4 and release v2.4.1"
        body = "Observed when upgrading from 1.0.0 to v2.4.1 on Django 4.2.1. File `models.py` fails."
        evidence = extract_issue_evidence(title, body, [])
        self.assertIn("models.py", evidence["files"])
        for item in ("3.10.4", "v2.4.1", "1.0.0", "4.2.1"):
            self.assertNotIn(item, evidence["files"])

    def test_extract_evidence_bare_filename_with_sentence_punctuation(self):
        text_cases = [
            "The error is in models.py.",
            "Please check models.py!",
            "Could this be in models.py?",
            "Found in models.py, views.py, and urls.py.",
            'Located in "models.py".',
            "Located in (models.py).",
            "Look at models.py: line 42",
        ]
        for text in text_cases:
            with self.subTest(text=text):
                evidence = extract_issue_evidence("Issue title", text, [])
                self.assertIn("models.py", evidence["files"])
                for f in evidence["files"]:
                    self.assertFalse(f.endswith("."))
                    self.assertFalse(f.endswith("!"))
                    self.assertFalse(f.endswith("?"))
                    self.assertFalse(f.endswith(","))
                    self.assertFalse(f.endswith(":"))

    @patch("core.issue_service._make_github_request")
    def test_fetch_github_issue_non_dict_response_raises_service_error(self, mock_request):
        mock_request.return_value = ["unexpected", "list", "response"]
        with self.assertRaises(GitHubServiceError) as cm:
            fetch_github_issue("django", "django", 123)
        self.assertIn("Unexpected response format", str(cm.exception))

    @patch("core.issue_service._make_github_request")
    def test_fetch_and_plan_closed_issue_metadata_and_notice(self, mock_request):
        mock_request.return_value = {
            "title": "Old bug resolved or rejected",
            "body": "Fixed in earlier version.",
            "state": "closed",
            "closed_at": "2026-09-01T12:00:00Z",
            "state_reason": "completed",
            "html_url": "https://github.com/django/django/issues/999",
            "user": {"login": "reporter"},
            "labels": [{"name": "bug"}],
            "comments": 2,
        }
        issue = fetch_github_issue("django", "django", 999)
        self.assertEqual(issue["state"], "closed")
        self.assertIsNotNone(issue["closed_at"])
        self.assertEqual(issue["state_reason"], "completed")

        plan = generate_contribution_plan(issue)
        self.assertEqual(plan["objective"]["state"], "closed")
        self.assertTrue(plan["objective"]["is_closed"])
        self.assertIsNotNone(plan["objective"]["closed_notice"])
        self.assertIn("CLOSED", plan["objective"]["closed_notice"])
        self.assertIn("check the issue comments and linked pull requests", plan["objective"]["closed_notice"].lower())
        # Ensure we do not assume every closed issue was resolved
        self.assertIn("duplicate", plan["objective"]["closed_notice"].lower())
        self.assertIn("Caution: This issue is closed", plan["implementation"]["disclaimer"])
        self.assertTrue(any("Verify Closed Status" in step for step in plan["investigation"]["steps"]))

    def test_django_guardian_1025_feature_regression(self):
        """
        Regression test for real-world issue #1025 on django-guardian/django-guardian:
        Title: 'Add an opt-in setting to let global permissions carry over to object checks'
        Labels: ['enhancement', 'documentation', 'more info required']
        Body: Feature request template describing opt-in setting.
        Must be categorized as Feature Request / Enhancement (NOT Documentation Improvement),
        must not recommend documentation-only implementation steps, and must handle zero-evidence safely.
        """
        issue_data = {
            "owner": "django-guardian",
            "name": "django-guardian",
            "issue_number": 1025,
            "title": "Add an opt-in setting to let global permissions carry over to object checks",
            "labels": [{"name": "enhancement"}, {"name": "documentation"}, {"name": "more info required"}],
            "body": (
                "**Is your feature request related to a problem? Please describe.**\n"
                "`has_perm(user, perm, obj)` currently treats global and object permissions as separate concepts, "
                "which makes it difficult to opt into a model where a global permission should also satisfy object-level checks."
            ),
            "state": "open",
        }

        plan = generate_contribution_plan(issue_data)

        # 1. Category must be Feature Request / Enhancement
        self.assertEqual(plan["objective"]["category"], "Feature Request / Enhancement")

        # 2. Evidence must not fabricate files
        self.assertFalse(plan["evidence"]["has_files"])
        self.assertEqual(plan["evidence"]["files"], [])

        # 3. Implementation must be feature-oriented, NOT documentation-only
        milestones = plan["implementation"]["milestones"]
        self.assertTrue(any("feature-issue-1025" in m for m in milestones))
        self.assertTrue(any("Search repository before editing" in m for m in milestones))
        self.assertTrue(any("Implement the requested functionality" in m for m in milestones))
        self.assertTrue(any("Update relevant documentation" in m for m in milestones))
        # Ensure it does not have documentation-only steps
        self.assertFalse(any("Apply precise documentation edits" in m for m in milestones))
        self.assertFalse(any("fixing grammar, terminology" in m for m in milestones))

        # 4. Investigation must recommend repository keyword search and inspection of tests
        investigation = plan["investigation"]["steps"]
        self.assertTrue(any("Repository Keyword Search" in step for step in investigation))
        self.assertTrue(any("Inspect Related Test Files" in step for step in investigation))
        self.assertTrue(any("Confirm Feature Alignment" in step for step in investigation))

        # 5. Testing must recommend searching existing test directories
        testing = plan["testing"]["steps"]
        self.assertTrue(any("Search existing test directories" in step for step in testing))

        # 6. PR guidelines must use feat prefix
        self.assertTrue(any("feat(django-guardian)" in g for g in plan["pull_request"]["guidelines"]))

    def test_classification_bug_fix_with_evidence(self):
        issue_data = {
            "owner": "encode",
            "name": "starlette",
            "issue_number": 123,
            "title": "Crash when passing None to json_encoder",
            "labels": [{"name": "bug"}],
            "body": "File \"src/encoder.py\", line 45\nTypeError: expected str, got None",
            "state": "open",
        }
        plan = generate_contribution_plan(issue_data)
        self.assertEqual(plan["objective"]["category"], "Bug Fix")
        self.assertTrue(plan["evidence"]["has_files"])
        self.assertIn("src/encoder.py", plan["evidence"]["files"])
        self.assertTrue(any("fix-issue-123" in m for m in plan["implementation"]["milestones"]))
        self.assertTrue(any("src/encoder.py" in m for m in plan["implementation"]["milestones"]))
        self.assertTrue(any("minimal, targeted patch" in m for m in plan["implementation"]["milestones"]))

    def test_classification_documentation_improvement(self):
        issue_data = {
            "owner": "django",
            "name": "django",
            "issue_number": 888,
            "title": "Fix typo in tutorial documentation",
            "labels": [{"name": "documentation"}],
            "body": "The tutorial has a typo in section 2.",
            "state": "open",
        }
        plan = generate_contribution_plan(issue_data)
        self.assertEqual(plan["objective"]["category"], "Documentation Improvement")
        self.assertTrue(any("docs-issue-888" in m for m in plan["implementation"]["milestones"]))
        self.assertTrue(any("precise documentation edits" in m for m in plan["implementation"]["milestones"]))
        self.assertTrue(any("Verify Documentation Build" in s for s in plan["testing"]["steps"]))

    def test_classification_refactoring_cleanup(self):
        issue_data = {
            "owner": "psf",
            "name": "requests",
            "issue_number": 456,
            "title": "Refactor session backend to reduce technical debt",
            "labels": [{"name": "refactoring"}, {"name": "tech-debt"}],
            "body": "Clean up internal session handlers and simplify class hierarchy.",
            "state": "open",
        }
        plan = generate_contribution_plan(issue_data)
        self.assertEqual(plan["objective"]["category"], "Refactoring / Cleanup")
        self.assertTrue(any("refactor-issue-456" in m for m in plan["implementation"]["milestones"]))
        self.assertTrue(any("Refactor code incrementally" in m for m in plan["implementation"]["milestones"]))
        self.assertTrue(any("Establish Test Baseline" in s for s in plan["investigation"]["steps"]))

    def test_classification_test_suite_improvement(self):
        issue_data = {
            "owner": "pallets",
            "name": "click",
            "issue_number": 789,
            "title": "Add unit tests for payment webhook handling",
            "labels": [{"name": "tests"}],
            "body": "We need test coverage for webhook failures.",
            "state": "open",
        }
        plan = generate_contribution_plan(issue_data)
        self.assertEqual(plan["objective"]["category"], "Test Suite Improvement")
        self.assertTrue(any("test-issue-789" in m for m in plan["implementation"]["milestones"]))
        self.assertTrue(any("automated test cases" in m for m in plan["implementation"]["milestones"]))
        self.assertTrue(any("Review Test Structure" in s for s in plan["investigation"]["steps"]))

    def test_classification_conflicting_and_ambiguous_signals(self):
        # 1. Doc title with Bug label -> Doc wins
        conflicting_doc = {
            "owner": "test", "name": "repo", "issue_number": 1,
            "title": "Fix typo in contributing guide",
            "labels": [{"name": "bug"}],
            "body": "Small typo on line 12 of CONTRIBUTING.md.",
            "state": "open",
        }
        plan_doc = generate_contribution_plan(conflicting_doc)
        self.assertEqual(plan_doc["objective"]["category"], "Documentation Improvement")

        # 2. Test title with Feature label -> Test wins
        conflicting_test = {
            "owner": "test", "name": "repo", "issue_number": 2,
            "title": "Add tests for user permissions",
            "labels": [{"name": "enhancement"}],
            "body": "Coverage needed for edge cases.",
            "state": "open",
        }
        plan_test = generate_contribution_plan(conflicting_test)
        self.assertEqual(plan_test["objective"]["category"], "Test Suite Improvement")

        # 3. Bug title with Doc label -> Bug wins
        conflicting_bug = {
            "owner": "test", "name": "repo", "issue_number": 3,
            "title": "Crash in routing table on empty route",
            "labels": [{"name": "documentation"}],
            "body": "Server panics when route is empty string.",
            "state": "open",
        }
        plan_bug = generate_contribution_plan(conflicting_bug)
        self.assertEqual(plan_bug["objective"]["category"], "Bug Fix")

        # 4. Ambiguous / No signals fallback -> General Improvement
        ambiguous = {
            "owner": "test", "name": "repo", "issue_number": 4,
            "title": "Discussion on upcoming roadmap",
            "labels": [],
            "body": "Meeting notes and open questions for the team.",
            "state": "open",
        }
        plan_ambiguous = generate_contribution_plan(ambiguous)
        self.assertEqual(plan_ambiguous["objective"]["category"], "General Improvement")
        self.assertTrue(any("patch-issue-4" in m for m in plan_ambiguous["implementation"]["milestones"]))

    def test_evidence_aware_recommendations_with_and_without_files(self):
        # With files
        with_files = {
            "owner": "test", "name": "repo", "issue_number": 10,
            "title": "Fix memory leak in router",
            "labels": [{"name": "bug"}],
            "body": "Memory leaks in `src/router.py` during route registration.",
            "state": "open",
        }
        plan_with_files = generate_contribution_plan(with_files)
        self.assertTrue(plan_with_files["evidence"]["has_files"])
        self.assertTrue(any("src/router.py" in step for step in plan_with_files["investigation"]["steps"]))
        self.assertTrue(any("src/router.py" in m for m in plan_with_files["implementation"]["milestones"]))
        self.assertTrue(any("src/router.py" in s for s in plan_with_files["testing"]["steps"]))

        # Without files
        without_files = {
            "owner": "test", "name": "repo", "issue_number": 11,
            "title": "Support oauth2 token refresh",
            "labels": [{"name": "enhancement"}],
            "body": "Please add oauth2 token refresh support.",
            "state": "open",
        }
        plan_without_files = generate_contribution_plan(without_files)
        self.assertFalse(plan_without_files["evidence"]["has_files"])
        self.assertTrue(any("Repository Keyword Search" in step for step in plan_without_files["investigation"]["steps"]))
        self.assertTrue(any("Inspect Related Test Files" in step for step in plan_without_files["investigation"]["steps"]))
        self.assertTrue(any("Search repository before editing" in m for m in plan_without_files["implementation"]["milestones"]))
        self.assertTrue(any("Search existing test directories" in s for s in plan_without_files["testing"]["steps"]))


class Issue2ContributionViewTests(TestCase):
    """
    Tests for Issue2Contribution controller (core.views.issue2contribution) and routing.
    All service calls are mocked; zero network calls are made.
    """

    def setUp(self):
        self.client = Client()
        AnalyzedRepository.objects.filter(owner="testpallets").delete()

    def tearDown(self):
        AnalyzedRepository.objects.filter(owner="testpallets").delete()

    def test_get_issue_empty_state_renders_successfully(self):
        response = self.client.get(reverse("issue2contribution"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Issue2Contribution")
        self.assertEqual(response.context["issue_url"], "")
        self.assertIsNone(response.context["issue"])
        self.assertIsNone(response.context["plan"])
        self.assertFalse(response.context["is_pull_request"])
        self.assertIsNone(response.context["error_message"])

    @patch("core.views.analyze_issue_and_generate_plan")
    def test_post_valid_issue_url_success(self, mock_analyze):
        mock_analyze.return_value = {
            "owner": "pallets",
            "name": "flask",
            "issue_number": 5000,
            "issue": {
                "title": "Bug in url_prefix handling",
                "body": "Detailed report...",
                "state": "open",
                "is_pull_request": False,
            },
            "plan": {
                "objective": {"summary": "Address issue #5000", "category": "Bug Fix"},
                "evidence": {"files": ["src/flask/blueprints.py"], "has_files": True},
            },
        }

        response = self.client.post(reverse("issue2contribution"), {
            "issue_url": "https://github.com/pallets/flask/issues/5000",
        })
        self.assertEqual(response.status_code, 200)
        mock_analyze.assert_called_once_with("https://github.com/pallets/flask/issues/5000")
        self.assertEqual(response.context["issue"]["title"], "Bug in url_prefix handling")
        self.assertIsNotNone(response.context["plan"])
        self.assertFalse(response.context["is_pull_request"])
        self.assertIsNone(response.context["error_message"])
        self.assertContains(response, "Bug in url_prefix handling")

    @patch("core.views.analyze_issue_and_generate_plan")
    def test_get_with_issue_url_query_param_success(self, mock_analyze):
        mock_analyze.return_value = {
            "owner": "pallets",
            "name": "flask",
            "issue_number": 5000,
            "issue": {
                "title": "Bug in url_prefix handling",
                "state": "open",
                "is_pull_request": False,
            },
            "plan": {
                "objective": {"summary": "Address issue #5000", "category": "Bug Fix"},
                "evidence": {"files": [], "has_files": False},
            },
        }

        response = self.client.get(reverse("issue2contribution"), {
            "issue_url": "pallets/flask#5000",
        })
        self.assertEqual(response.status_code, 200)
        mock_analyze.assert_called_once_with("pallets/flask#5000")
        self.assertEqual(response.context["issue"]["title"], "Bug in url_prefix handling")

    @patch("core.views.analyze_issue_and_generate_plan")
    def test_post_invalid_url_displays_error(self, mock_analyze):
        mock_analyze.side_effect = InvalidURLError("Invalid host. The URL must point to github.com.")

        response = self.client.post(reverse("issue2contribution"), {
            "issue_url": "https://evil.com/owner/repo/issues/1",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("Invalid host", response.context["error_message"])
        self.assertContains(response, "Invalid host")

    @patch("core.views.analyze_issue_and_generate_plan")
    def test_post_pull_request_url_displays_notice(self, mock_analyze):
        mock_analyze.side_effect = PullRequestNotSupportedError(
            "The provided URL points to a Pull Request, not an Issue. Please provide a GitHub Issue URL."
        )

        response = self.client.post(reverse("issue2contribution"), {
            "issue_url": "https://github.com/django/django/pull/123",
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_pull_request"])
        self.assertIn("Pull Request", response.context["error_message"])
        self.assertContains(response, "Pull Request")

    @patch("core.views.analyze_issue_and_generate_plan")
    def test_post_pull_request_payload_detected(self, mock_analyze):
        mock_analyze.return_value = {
            "owner": "pallets",
            "name": "flask",
            "issue_number": 99,
            "issue": {
                "title": "Refactor router",
                "state": "open",
                "is_pull_request": True,
            },
            "plan": None,
        }

        response = self.client.post(reverse("issue2contribution"), {
            "issue_url": "pallets/flask#99",
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_pull_request"])
        self.assertIsNone(response.context["plan"])
        self.assertIn("Pull Request", response.context["error_message"])

    @patch("core.views.analyze_issue_and_generate_plan")
    def test_post_issue_not_found_displays_error(self, mock_analyze):
        mock_analyze.side_effect = IssueNotFoundError("Issue #99999 was not found on GitHub or the repository is private.")

        response = self.client.post(reverse("issue2contribution"), {
            "issue_url": "django/django#99999",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("not found on GitHub", response.context["error_message"])
        self.assertContains(response, "not found on GitHub")

    @patch("core.views.analyze_issue_and_generate_plan")
    def test_post_rate_limit_and_network_errors(self, mock_analyze):
        mock_analyze.side_effect = RateLimitError("GitHub API rate limit exceeded")
        resp_rate = self.client.post(reverse("issue2contribution"), {"issue_url": "django/django#10"})
        self.assertEqual(resp_rate.status_code, 200)
        self.assertIn("rate limit exceeded", resp_rate.context["error_message"].lower())
        self.assertContains(resp_rate, "rate limit exceeded")

        mock_analyze.side_effect = NetworkError("Connection timed out")
        resp_net = self.client.post(reverse("issue2contribution"), {"issue_url": "django/django#10"})
        self.assertEqual(resp_net.status_code, 200)
        self.assertIn("network error", resp_net.context["error_message"].lower())
        self.assertContains(resp_net, "Network error")

    @patch("core.views.analyze_issue_and_generate_plan")
    def test_post_authentication_and_generic_service_error(self, mock_analyze):
        mock_analyze.side_effect = AuthenticationError("Bad credentials")
        resp_auth = self.client.post(reverse("issue2contribution"), {"issue_url": "django/django#10"})
        self.assertEqual(resp_auth.status_code, 200)
        self.assertIn("authentication failed", resp_auth.context["error_message"].lower())
        self.assertContains(resp_auth, "authentication failed")

        mock_analyze.side_effect = GitHubServiceError("Unexpected upstream status 500")
        resp_err = self.client.post(reverse("issue2contribution"), {"issue_url": "django/django#10"})
        self.assertEqual(resp_err.status_code, 200)
        self.assertIn("GitHub service error", resp_err.context["error_message"])
        self.assertContains(resp_err, "GitHub service error")
        self.assertNotIn("status 500", resp_err.context["error_message"])
        self.assertNotIn("Unexpected upstream", resp_err.content.decode())

    def test_post_empty_and_whitespace_url_displays_validation_error(self):
        cases = [
            {},
            {"issue_url": ""},
            {"issue_url": "   "},
            {"url": ""},
            {"url": " \t \n "},
        ]
        for data in cases:
            with self.subTest(data=data):
                response = self.client.post(reverse("issue2contribution"), data)
                self.assertEqual(response.status_code, 200)
                expected_msg = "Please provide a valid GitHub issue URL or owner/repo#number."
                self.assertEqual(response.context["error_message"], expected_msg)
                self.assertContains(response, expected_msg)
                self.assertIsNone(response.context["issue"])
                self.assertIsNone(response.context["plan"])

    @patch("core.views.logger.exception")
    @patch("core.views.analyze_issue_and_generate_plan")
    def test_post_unexpected_exception_logged_and_sanitized(self, mock_analyze, mock_log_exc):
        mock_analyze.side_effect = RuntimeError("Internal unexpected failure")
        response = self.client.post(reverse("issue2contribution"), {"issue_url": "pallets/flask#10"})
        self.assertEqual(response.status_code, 200)
        mock_log_exc.assert_called_once()
        expected_msg = "An unexpected error occurred while analyzing the issue. Please try again."
        self.assertEqual(response.context["error_message"], expected_msg)
        self.assertContains(response, expected_msg)
        self.assertNotContains(response, "Internal unexpected failure")

    def test_unsupported_http_methods_rejected(self):
        resp_put = self.client.put(reverse("issue2contribution"))
        self.assertEqual(resp_put.status_code, 405)

        resp_del = self.client.delete(reverse("issue2contribution"))
        self.assertEqual(resp_del.status_code, 405)

    @patch("core.views.analyze_issue_and_generate_plan")
    def test_post_links_existing_analyzed_repository_context(self, mock_analyze):
        repo = AnalyzedRepository.objects.create(
            owner="testpallets",
            name="flask",
            url="https://github.com/testpallets/flask",
            readiness_score=88.5,
            primary_language="Python",
        )

        mock_analyze.return_value = {
            "owner": "testpallets",
            "name": "flask",
            "issue_number": 5000,
            "issue": {"title": "Test Issue", "state": "open", "is_pull_request": False},
            "plan": {"objective": {"summary": "Plan summary"}, "evidence": {}},
        }

        response = self.client.post(reverse("issue2contribution"), {"issue_url": "testpallets/flask#5000"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context["repo_context"])
        self.assertEqual(response.context["repo_context"].id, repo.id)
        self.assertEqual(response.context["repo_context"].readiness_score, 88.5)

    @patch("core.views.analyze_issue_and_generate_plan")
    def test_post_without_existing_analyzed_repository_context_is_none(self, mock_analyze):
        mock_analyze.return_value = {
            "owner": "unknownorg",
            "name": "unknownrepo",
            "issue_number": 1,
            "issue": {"title": "Unknown", "state": "open", "is_pull_request": False},
            "plan": {"objective": {"summary": "Plan summary"}, "evidence": {}},
        }

        response = self.client.post(reverse("issue2contribution"), {"issue_url": "unknownorg/unknownrepo#1"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["repo_context"])

    def test_page_renders_with_csrf_token_and_form(self):
        response = self.client.get(reverse("issue2contribution"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertContains(response, 'id="issue_url"')
        self.assertContains(response, "Analyze Issue")
        self.assertContains(response, "Repository Analysis")
        self.assertContains(response, "OSS Navigator")
        self.assertContains(response, "OSS Mentor")
        self.assertContains(response, "Issue2Contribution")

    @patch("core.views.analyze_issue_and_generate_plan")
    def test_post_preserves_submitted_issue_url_in_form(self, mock_analyze):
        mock_analyze.return_value = {
            "owner": "pallets",
            "name": "flask",
            "issue_number": 5000,
            "issue": {"title": "Issue title", "state": "open", "is_pull_request": False},
            "plan": {"objective": {"summary": "Summary"}, "evidence": {}},
        }
        submitted_url = "https://github.com/pallets/flask/issues/5000"
        response = self.client.post(reverse("issue2contribution"), {"issue_url": submitted_url})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["issue_url"], submitted_url)
        self.assertContains(response, f'value="{submitted_url}"')

    @patch("core.views.analyze_issue_and_generate_plan")
    def test_post_successful_analysis_renders_all_six_plan_sections_and_evidence(self, mock_analyze):
        mock_analyze.return_value = {
            "owner": "pallets",
            "name": "flask",
            "issue_number": 5000,
            "issue": {
                "title": "Bug in url_prefix handling",
                "body": "Detailed report...",
                "state": "open",
                "author": "tester",
                "author_avatar": "https://avatars.githubusercontent.com/u/1",
                "author_url": "https://github.com/tester",
                "labels": [{"name": "bug", "color": "d73a4a", "description": "Bug label"}],
                "comments_count": 3,
                "is_pull_request": False,
            },
            "plan": {
                "objective": {
                    "title": "Issue Objective & Scope",
                    "category": "Bug Fix",
                    "state": "open",
                    "is_good_first_issue": True,
                    "summary": "Address issue #5000",
                    "explanation": "Detailed explanation of bug fix",
                },
                "investigation": {
                    "title": "Pre-Coding Investigation",
                    "steps": ["Step A: Review Discussions", "Step B: Verify Failure Locally"],
                },
                "evidence": {
                    "title": "Relevant Files & Component Evidence",
                    "has_files": True,
                    "files": ["src/flask/blueprints.py"],
                    "symbols": ["Blueprint.register()"],
                    "errors": ["TypeError: url_prefix must be str"],
                    "component_labels": ["area:routing"],
                    "notice": "Inspect these files first as direct starting points:",
                },
                "implementation": {
                    "title": "Suggested Implementation Path",
                    "milestones": ["Milestone 1: Create feature branch", "Milestone 2: Apply patch"],
                    "disclaimer": "Do not assume root cause without testing.",
                },
                "testing": {
                    "title": "Testing & Verification Guidance",
                    "steps": ["Test step 1: Add automated regression test", "Test step 2: Run test suite"],
                },
                "pull_request": {
                    "title": "Pull Request Preparation",
                    "closing_keyword": "Fixes #5000",
                    "guidelines": ["Guideline 1: Atomic commits", "Guideline 2: Reference issue"],
                },
            },
        }

        response = self.client.post(reverse("issue2contribution"), {
            "issue_url": "pallets/flask#5000",
        })
        self.assertEqual(response.status_code, 200)

        # Verify all 6 plan section titles rendered
        self.assertContains(response, "Issue Objective &amp; Scope")
        self.assertContains(response, "Pre-Coding Investigation")
        self.assertContains(response, "Relevant Files &amp; Component Evidence")
        self.assertContains(response, "Suggested Implementation Path")
        self.assertContains(response, "Testing &amp; Verification Guidance")
        self.assertContains(response, "Pull Request Preparation")

        # Verify evidence rendered
        self.assertContains(response, "src/flask/blueprints.py")
        self.assertContains(response, "Blueprint.register()")
        self.assertContains(response, "TypeError: url_prefix must be str")
        self.assertContains(response, "area:routing")

        # Verify closing keyword and safe copy-button attributes
        self.assertContains(response, "Fixes #5000")
        self.assertContains(response, 'data-copy-text="src/flask/blueprints.py"')
        self.assertContains(response, 'data-copy-text="Fixes #5000"')
        self.assertNotContains(response, 'onclick="navigator.clipboard')

    def test_all_navbars_include_issue2contribution_link(self):
        resp_home = self.client.get(reverse("home"))
        self.assertEqual(resp_home.status_code, 200)
        self.assertContains(resp_home, 'href="/issue/"')

        resp_nav = self.client.get(reverse("navigator"))
        self.assertEqual(resp_nav.status_code, 200)
        self.assertContains(resp_nav, 'href="/issue/"')

        resp_mentor = self.client.get(reverse("mentor"))
        self.assertEqual(resp_mentor.status_code, 200)
        self.assertContains(resp_mentor, 'href="/issue/"')
