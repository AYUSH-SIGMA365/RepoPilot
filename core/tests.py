import urllib.error
from unittest.mock import patch, MagicMock
from django.test import TestCase, Client
from django.urls import reverse
from .models import AnalyzedRepository
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
