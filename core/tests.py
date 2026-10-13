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
