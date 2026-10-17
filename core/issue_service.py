"""
Issue2Contribution service for RepoPilot.
Safely parses GitHub issue URLs, fetches issue details via the existing GitHub client,
extracts evidence-based metadata without hallucination, and generates a structured
six-part beginner contribution plan.
"""

import os
import re
import urllib.parse
from django.utils.dateparse import parse_datetime
from .github_service import (
    _make_github_request,
    GitHubServiceError,
    InvalidURLError,
    RepositoryNotFoundError,
    RateLimitError,
    AuthenticationError,
    NetworkError,
)


class IssueNotFoundError(RepositoryNotFoundError):
    """Raised when the specified issue does not exist or the repository is private."""
    pass


class PullRequestNotSupportedError(InvalidURLError):
    """Raised when a user submits a Pull Request URL instead of an Issue URL."""
    pass


# Deterministic Contribution Plan Categories
CATEGORY_BUG = "Bug Fix"
CATEGORY_FEATURE = "Feature Request / Enhancement"
CATEGORY_DOC = "Documentation Improvement"
CATEGORY_REFACTOR = "Refactoring / Cleanup"
CATEGORY_TEST = "Test Suite Improvement"
CATEGORY_GENERAL = "General Improvement"


def parse_github_issue_url(input_str: str) -> tuple[str, str, int]:
    """
    Safely validates and extracts repository owner, name, and issue number.
    Supports formats:
      - https://github.com/owner/repo/issues/123
      - http://github.com/owner/repo/issues/123/
      - github.com/owner/repo/issues/123
      - https://github.com/owner/repo/issues/123#issuecomment-456
      - owner/repo#123 (shorthand)

    Validates hostname, owner, repository, and positive issue number.
    Rejects pull request URLs and non-GitHub hosts.
    Returns (owner, name, issue_number).
    """
    if not input_str or not isinstance(input_str, str):
        raise InvalidURLError("Please provide a valid GitHub issue URL or owner/repo#number.")

    clean_input = input_str.strip()

    # 1. Check for shorthand format: owner/repo#123
    shorthand_match = re.match(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#(\d+)$", clean_input)
    if shorthand_match:
        owner, name, num_str = shorthand_match.groups()
        issue_number = int(num_str)
        if issue_number <= 0:
            raise InvalidURLError("Invalid issue number. The issue number must be a positive integer.")
        return owner, name, issue_number

    # 2. Normalize URL for standard parsing
    # Strip URL fragments (e.g. #issuecomment-...)
    if "#" in clean_input:
        clean_input = clean_input.split("#", 1)[0]

    # Explicit scheme validation: accept only HTTP and HTTPS for full URLs
    if "://" in clean_input:
        lower_input = clean_input.lower()
        if not (lower_input.startswith("https://") or lower_input.startswith("http://")):
            raise InvalidURLError("Unsupported URL scheme. Only HTTP and HTTPS GitHub URLs are supported.")
    else:
        # Scheme-less URL format (e.g., github.com/owner/repo/issues/123)
        clean_input = "https://" + clean_input

    try:
        parsed = urllib.parse.urlparse(clean_input)
    except Exception:
        raise InvalidURLError("Malformed URL. Please provide a valid GitHub issue URL.")

    # Re-verify parsed scheme for security
    if (parsed.scheme or "").lower() not in ("http", "https"):
        raise InvalidURLError("Unsupported URL scheme. Only HTTP and HTTPS GitHub URLs are supported.")

    hostname = (parsed.hostname or "").lower()
    if hostname not in ("github.com", "www.github.com"):
        raise InvalidURLError("Invalid host. The URL must point to github.com.")

    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]

    parts = [p for p in path.split("/") if p]

    # Explicit check for pull request URLs
    if len(parts) >= 3 and parts[2].lower() in ("pull", "pulls"):
        raise PullRequestNotSupportedError(
            "The provided URL points to a Pull Request, not an Issue. "
            "Please provide a GitHub Issue URL (e.g., https://github.com/owner/repo/issues/123)."
        )

    # Expected: ['owner', 'repo', 'issues', '<number>']
    if len(parts) != 4 or parts[2].lower() != "issues":
        raise InvalidURLError(
            "Invalid URL format. Expected: https://github.com/owner/repository/issues/123 or owner/repo#123"
        )

    owner = parts[0]
    name = parts[1]
    num_str = parts[3]

    # Validate characters (alphanumeric, hyphens, underscores, dots)
    valid_name_pattern = re.compile(r"^[A-Za-z0-9_.-]+$")
    if not valid_name_pattern.match(owner) or not valid_name_pattern.match(name):
        raise InvalidURLError("Repository owner and name must contain only alphanumeric characters, dashes, dots, or underscores.")

    if not num_str.isdigit():
        raise InvalidURLError("Invalid issue number. The issue number must be a positive integer.")

    issue_number = int(num_str)
    if issue_number <= 0:
        raise InvalidURLError("Invalid issue number. The issue number must be a positive integer.")

    return owner, name, issue_number


def fetch_github_issue(owner: str, name: str, issue_number: int) -> dict:
    """
    Retrieves public GitHub issue details via the existing _make_github_request client.
    Never calls user-supplied URLs directly; builds the endpoint internally.
    Distinguishes Pull Requests from ordinary issues using the API response payload.
    """
    api_url = f"https://api.github.com/repos/{owner}/{name}/issues/{issue_number}"

    issue_data = _make_github_request(api_url)
    if issue_data is None:
        raise IssueNotFoundError(f"Issue #{issue_number} in '{owner}/{name}' was not found on GitHub or the repository is private.")

    if not isinstance(issue_data, dict):
        raise GitHubServiceError("Unexpected response format from GitHub API.")

    # In GitHub REST API, all pull requests are returned by the /issues endpoint,
    # but contain a 'pull_request' dictionary key with PR URLs.
    is_pull_request = bool("pull_request" in issue_data and issue_data["pull_request"])

    # Labels
    raw_labels = issue_data.get("labels", [])
    labels = []
    if isinstance(raw_labels, list):
        for lbl in raw_labels:
            if isinstance(lbl, dict):
                labels.append({
                    "name": lbl.get("name", ""),
                    "color": lbl.get("color", "6c757d"),
                    "description": lbl.get("description") or "",
                })
            elif isinstance(lbl, str):
                labels.append({"name": lbl, "color": "6c757d", "description": ""})

    # Dates
    created_at = parse_datetime(issue_data.get("created_at") or "")
    updated_at = parse_datetime(issue_data.get("updated_at") or "")
    closed_at = parse_datetime(issue_data.get("closed_at") or "") if issue_data.get("closed_at") else None

    user_info = issue_data.get("user") or {}

    return {
        "owner": owner,
        "name": name,
        "issue_number": issue_number,
        "title": (issue_data.get("title") or "").strip(),
        "body": issue_data.get("body") or "",
        "state": issue_data.get("state", "open"),
        "state_reason": issue_data.get("state_reason"),
        "html_url": issue_data.get("html_url") or f"https://github.com/{owner}/{name}/issues/{issue_number}",
        "author": user_info.get("login", "unknown"),
        "author_avatar": user_info.get("avatar_url"),
        "author_url": user_info.get("html_url"),
        "labels": labels,
        "comments_count": int(issue_data.get("comments") or 0),
        "created_at": created_at,
        "updated_at": updated_at,
        "closed_at": closed_at,
        "is_pull_request": is_pull_request,
    }


def extract_issue_evidence(title: str, body: str, labels: list[dict]) -> dict:
    """
    Extracts strictly evidence-backed technical details from the issue title, body, and labels.
    Never invents likely files, root causes, or stack locations without direct textual evidence.
    """
    text = f"{title or ''}\n{body or ''}"

    SOURCE_EXTENSIONS = (
        r"py|js|ts|tsx|jsx|html|css|scss|json|yaml|yml|md|rst|toml|ini|cfg|sh|sql|go|rs|c|cpp|h|java|rb|php|vue|svelte|xml"
    )

    # 1. Extract file paths from stack traces and code references
    evidenced_files = []

    # Match Python tracebacks: File "path/to/file.py", line 123
    py_tb_files = re.findall(r'File ["\']([^"\']+\.[A-Za-z0-9]+)["\'], line \d+', text)
    evidenced_files.extend(py_tb_files)

    # Match JS/TS/Node tracebacks: at ... (/path/to/file.js:12:3) or at /path/to/file.js:12:3
    js_tb_files = re.findall(r'^\s*at (?:[^\s(]+ \()?([A-Za-z0-9_./\\-]+\.[A-Za-z0-9]+):(?:\d+)(?::\d+)?\)?', text, re.MULTILINE)
    evidenced_files.extend(js_tb_files)

    # Match explicit files in backticks: `path/to/file.ext` or `filename.ext`
    backtick_files = re.findall(
        rf'`([A-Za-z0-9_./\\-]+\.(?:{SOURCE_EXTENSIONS}))`',
        text
    )
    evidenced_files.extend(backtick_files)

    # Match clean paths in code blocks or diff headers (e.g. +++ b/path/to/file.py or --- a/path/to/file.py)
    diff_files = re.findall(r'(?:---|\+\+\+)\s+[ab]/([A-Za-z0-9_./\\-]+\.[A-Za-z0-9]+)', text)
    evidenced_files.extend(diff_files)

    # Exclude diff header lines (including indented code blocks) from bare file matching to avoid duplicate 'b/path' prefixes
    non_diff_text = re.sub(r'^\s*(?:---|\+\+\+)\s+[ab]/\S+', '', text, flags=re.MULTILINE)

    # Match bare file references in prose with recognized source code extensions
    # Lookahead allows trailing sentence punctuation (., !, ?, :, ,, ;) or closing brackets/quotes
    bare_files = re.findall(
        rf'(?:^|[\s(\[\"\'<])([A-Za-z0-9_./\\-]+\.(?:{SOURCE_EXTENSIONS}))(?=$|[\s)\]\"\'\>\,\:\;\?\!]|\.(?:\s|$))',
        non_diff_text
    )
    evidenced_files.extend(bare_files)

    # Clean and filter candidate paths
    valid_files = []
    seen_files = set()
    excluded_extensions = {"png", "jpg", "jpeg", "gif", "svg", "webp", "pdf", "zip", "tar", "gz", "bmp", "ico", "tiff"}

    for raw_f in evidenced_files:
        f = raw_f.strip().strip("'\"`").replace("\\", "/")
        # Strip any trailing punctuation so sentence-ending periods are never in the file path
        f = f.rstrip(".,;:!?")
        # Exclude URLs or protocol schemes
        if f.startswith(("http:", "https:", "ftp:")) or "://" in f:
            continue
        # Exclude image/asset attachments frequently found in issue descriptions
        ext = f.split(".")[-1].lower() if "." in f else ""
        if ext in excluded_extensions:
            continue
        # Exclude dotfiles without names or version strings like 1.2.3.4 or v2.4.1
        if f in (".", "..") or re.match(r"^[vV]?\d+(\.\d+)+$", f):
            continue
        # Ignore diff artifacts with a/ or b/ prefixes if the clean base file is already known
        if (f.startswith("a/") or f.startswith("b/")) and f[2:] in seen_files:
            continue

        if f and f not in seen_files:
            seen_files.add(f)
            valid_files.append(f)

    # 2. Extract code symbols in backticks (function names, classes, methods)
    raw_symbols = re.findall(r'`([A-Za-z_][A-Za-z0-9_.]*(?:\(\))?)`', text)
    common_non_symbols = {
        "get", "post", "put", "delete", "true", "false", "null", "none",
        "pip", "npm", "git", "python", "self", "str", "int", "dict", "list",
        "bool", "def", "class", "import", "return", "if", "else", "for", "in",
        "bash", "sh", "yarn", "docker", "main", "master", "run", "start",
    }
    valid_symbols = []
    seen_symbols = set()
    for s in raw_symbols:
        s_clean = s.strip()
        s_lower = s_clean.lower().rstrip("()")
        if s_lower not in common_non_symbols and len(s_clean) >= 3 and s_clean not in seen_symbols:
            # If symbol contains a file extension already in valid_files, don't duplicate as symbol
            if any(s_clean.endswith(f".{ext}") for ext in ("py", "js", "ts", "json", "yml")):
                continue
            seen_symbols.add(s_clean)
            valid_symbols.append(s_clean)
            if len(valid_symbols) >= 6:
                break

    # 3. Extract explicit error messages
    error_matches = re.findall(r'([A-Za-z0-9_]+(?:Error|Exception|Warning|Fault)):\s*([^\n\r]+)', text)
    explicit_errors = []
    for err_cls, err_msg in error_matches:
        full_err = f"{err_cls}: {err_msg.strip()}"
        if full_err not in explicit_errors:
            explicit_errors.append(full_err[:120])
            if len(explicit_errors) >= 3:
                break

    # 4. Analyze Labels and Metadata
    label_names = [lbl.get("name", "").strip().lower() for lbl in labels if isinstance(lbl, dict)]
    is_good_first_issue = any(
        bool(re.search(r'\b(good first issue|good-first-issue|beginner|easy|starter|help wanted|help-wanted)\b', name))
        for name in label_names
    )

    component_labels = [
        lbl.get("name") for lbl in labels if isinstance(lbl, dict)
        and any(prefix in lbl.get("name", "").lower() for prefix in ("area:", "component:", "module:", "pkg/", "subsystem:"))
    ]

    # Deterministic Multi-Signal Category Scoring
    category_scores = {
        CATEGORY_BUG: 0,
        CATEGORY_FEATURE: 0,
        CATEGORY_DOC: 0,
        CATEGORY_REFACTOR: 0,
        CATEGORY_TEST: 0,
    }

    # Signal 1: Labels
    has_bug_label = any(bool(re.search(r'\b(bug|defect|crash|regression|broken|error|fault)\b|^type:\s*bug|^kind/bug', name)) for name in label_names)
    has_feature_label = any(bool(re.search(r'\b(feature|enhancement|proposal|new-feature|feature-request)\b|^type:\s*(?:feature|enhancement)|^kind/(?:feature|enhancement)', name)) for name in label_names)
    has_doc_label = any(bool(re.search(r'\b(doc|docs|documentation|typo|readme)\b|^type:\s*documentation|^kind/documentation', name)) for name in label_names)
    has_refactor_label = any(bool(re.search(r'\b(refactor|refactoring|cleanup|clean-up|tech-debt|technical-debt|code-quality|modernize|simplification|deprecation)\b', name)) for name in label_names)
    has_test_label = any(bool(re.search(r'\b(test|tests|testing|test-suite|qa|coverage|ci|integration-test|unit-test)\b', name)) for name in label_names)

    if has_bug_label:
        category_scores[CATEGORY_BUG] += 10
    if has_feature_label:
        category_scores[CATEGORY_FEATURE] += 10
    if has_refactor_label:
        category_scores[CATEGORY_REFACTOR] += 10
    if has_test_label:
        category_scores[CATEGORY_TEST] += 10
    if has_doc_label:
        # If code feature/bug/refactor/test is also labeled, documentation is often a secondary task
        if has_feature_label or has_bug_label or has_refactor_label or has_test_label:
            category_scores[CATEGORY_DOC] += 4
        else:
            category_scores[CATEGORY_DOC] += 10

    # Signal 2: Title Phrases & Keywords
    title_lower = title.lower().strip()

    # Documentation title patterns
    if re.search(r'\b(?:fix\s+typo|typo\s+in|update\s+(?:docs?|readme|documentation|tutorial)|improve\s+(?:docs?|readme|documentation)|clarify\s+(?:docs?|documentation)|documentation\s+update|missing\s+docs?|document\b)', title_lower):
        category_scores[CATEGORY_DOC] += 12
    elif re.search(r'\b(?:doc|docs|readme|documentation|typo|docstring|docstrings|tutorial)\b', title_lower):
        category_scores[CATEGORY_DOC] += 6

    # Test title patterns
    if re.search(r'\b(?:add\s+tests?|write\s+tests?|unit\s+tests?|regression\s+tests?|test\s+coverage|flaky\s+tests?|fix\s+tests?|e2e\s+tests?|integration\s+tests?)\b', title_lower):
        category_scores[CATEGORY_TEST] += 12
    elif re.search(r'\b(?:tests?|testing|coverage|unskip|pytest|unittest)\b', title_lower):
        category_scores[CATEGORY_TEST] += 6

    # Refactor title patterns
    if re.search(r'\b(?:refactor|refactoring|clean\s*up|cleanup|tech\s*debt|technical\s*debt|code\s*quality|modernize|deprecate|remove\s+deprecated|simplif(?:y|ication))\b', title_lower):
        category_scores[CATEGORY_REFACTOR] += 12

    # Feature title patterns
    if re.search(r'^(?:add\b(?![\s\w]*(?:tests?|docs?|readme|documentation))|allow\b|support\b|implement\b|introduce\b|enable\b|provide\b)|(?:feature\s+request|feature\b|proposal\b|opt-in\b|option\s+for|new\s+setting|capability\b)', title_lower):
        category_scores[CATEGORY_FEATURE] += 10
    elif re.search(r'\b(?:add(?![\s\w]*(?:tests?|docs?|readme|documentation))|support|allow|implement|proposal|opt-in|setting)\b', title_lower):
        category_scores[CATEGORY_FEATURE] += 6

    # Bug title patterns
    if re.search(r'^(?:fix\b(?![\s\w]*(?:typo|docs?|readme|documentation|tests?))|resolve\b|patch\b)|(?:crash|crashes|broken|fails|failure|exception|traceback|panic|leak|infinite\s+loop|regression\b)', title_lower):
        category_scores[CATEGORY_BUG] += 10
    elif re.search(r'\b(?:fix(?![\s\w]*(?:typo|docs?|readme|documentation|tests?))|bug|error|broken|fails|fault)\b', title_lower):
        category_scores[CATEGORY_BUG] += 6

    # Signal 3: Body Markers & Issue Template Hints
    body_lower = (body or "").lower()
    if re.search(r'\b(?:is\s+your\s+feature\s+request\s+related\s+to\s+a\s+problem|describe\s+the\s+solution\s+you(?:\'d|\s+would)\s+like|proposed\s+(?:solution|feature|change)|feature\s+description|motivation)\b', body_lower):
        category_scores[CATEGORY_FEATURE] += 8

    if re.search(r'\b(?:describe\s+the\s+bug|steps\s+to\s+reproduce|expected\s+behavior|actual\s+behavior|traceback\s+\(most\s+recent\s+call\s+last\))\b', body_lower):
        category_scores[CATEGORY_BUG] += 8
    if explicit_errors:
        category_scores[CATEGORY_BUG] += 6

    if re.search(r'\b(?:documentation\s+(?:bug|issue|error)|typo\s+in\s+docs?|clarify\s+in\s+docs?|outdated\s+documentation)\b', body_lower):
        category_scores[CATEGORY_DOC] += 8

    if re.search(r'\b(?:code\s+refactoring|clean\s+up\s+internal|reduce\s+technical\s+debt|modernize\s+codebase)\b', body_lower):
        category_scores[CATEGORY_REFACTOR] += 8

    if re.search(r'\b(?:increase\s+test\s+coverage|flaky\s+test\s+report|add\s+unit\s+tests\s+for)\b', body_lower):
        category_scores[CATEGORY_TEST] += 8

    # Select Primary Category
    priority_order = [CATEGORY_FEATURE, CATEGORY_BUG, CATEGORY_REFACTOR, CATEGORY_TEST, CATEGORY_DOC]
    best_category = max(
        priority_order,
        key=lambda cat: (category_scores[cat], -priority_order.index(cat))
    )

    if category_scores[best_category] > 0:
        primary_category = best_category
    else:
        primary_category = CATEGORY_GENERAL

    has_doc_signal = bool(has_doc_label or category_scores[CATEGORY_DOC] > 0)

    return {
        "files": valid_files,
        "has_files": len(valid_files) > 0,
        "symbols": valid_symbols,
        "errors": explicit_errors,
        "component_labels": component_labels,
        "category": primary_category,
        "category_scores": category_scores,
        "is_bug": (primary_category == CATEGORY_BUG),
        "is_feature": (primary_category == CATEGORY_FEATURE),
        "is_doc": (primary_category == CATEGORY_DOC),
        "is_refactor": (primary_category == CATEGORY_REFACTOR),
        "is_test": (primary_category == CATEGORY_TEST),
        "is_good_first_issue": is_good_first_issue,
        "has_doc_signal": has_doc_signal,
    }


def generate_contribution_plan(issue_data: dict, repo_context: dict | None = None) -> dict:
    """
    Constructs a deterministic, evidence-grounded six-part contribution plan.
    Does not hallucinate files or declare unproven root causes.
    """
    title = issue_data.get("title", "")
    body = issue_data.get("body", "")
    labels = issue_data.get("labels", [])
    issue_number = issue_data.get("issue_number")
    state = issue_data.get("state", "open")
    owner = issue_data.get("owner", "owner")
    name = issue_data.get("name", "repo")

    evidence = extract_issue_evidence(title, body, labels)

    # 1. Determine Category
    category = evidence["category"]
    is_bug = (category == CATEGORY_BUG)
    is_feature = (category == CATEGORY_FEATURE)
    is_doc = (category == CATEGORY_DOC)
    is_refactor = (category == CATEGORY_REFACTOR)
    is_test = (category == CATEGORY_TEST)

    # Part 1: Objective & Scope
    objective_summary = f"Address issue #{issue_number}: {title}"
    if is_bug:
        if evidence["has_files"] or evidence["symbols"]:
            targets = ", ".join((evidence["files"] + evidence["symbols"])[:3])
            objective_explanation = (
                f"This issue reports an unexpected error or defect in {owner}/{name} involving {targets}. "
                "The objective is to reliably reproduce the failure, isolate the root cause, "
                "and implement a focused patch with regression tests."
            )
        else:
            objective_explanation = (
                f"This issue reports an unexpected error or defect in {owner}/{name}. "
                "The objective is to reliably reproduce the failure, isolate the root cause, "
                "and implement a focused patch with regression tests."
            )
    elif is_feature:
        if evidence["has_files"] or evidence["symbols"]:
            targets = ", ".join((evidence["files"] + evidence["symbols"])[:3])
            objective_explanation = (
                f"This issue requests a new capability or enhancement for {owner}/{name} involving {targets}. "
                "The objective is to understand the intended behavior, confirm project alignment with maintainers, "
                "and build the requested functionality cleanly."
            )
        else:
            objective_explanation = (
                f"This issue requests a new capability or enhancement for {owner}/{name}. "
                "The objective is to understand the intended behavior, confirm project alignment with maintainers, "
                "and build the requested functionality cleanly."
            )
    elif is_doc:
        objective_explanation = (
            f"This issue identifies missing, unclear, or incorrect documentation in {owner}/{name}. "
            "The objective is to clarify instructions, fix typos, or add missing reference material."
        )
    elif is_refactor:
        if evidence["has_files"] or evidence["symbols"]:
            targets = ", ".join((evidence["files"] + evidence["symbols"])[:3])
            objective_explanation = (
                f"This issue proposes code refactoring or cleanup in {owner}/{name} targeting {targets}. "
                "The objective is to improve code structure and maintainability while preserving existing behavior."
            )
        else:
            objective_explanation = (
                f"This issue proposes code refactoring or cleanup in {owner}/{name}. "
                "The objective is to improve code structure and maintainability while preserving existing behavior."
            )
    elif is_test:
        objective_explanation = (
            f"This issue focuses on test suite improvements or test coverage for {owner}/{name}. "
            "The objective is to add automated test cases, stabilize tests, or expand test coverage."
        )
    else:
        objective_explanation = (
            f"This issue outlines a contribution task for {owner}/{name}. "
            "The objective is to review the description and deliver the requested improvement."
        )

    is_closed = (str(state).lower() == "closed")
    closed_notice = None
    if is_closed:
        closed_notice = (
            f"Notice: Issue #{issue_number} is currently marked as CLOSED on GitHub. "
            "It may have already been resolved by an existing pull request, closed as a duplicate, "
            "or determined to be not reproducible or out of scope. Before starting any work, "
            "check the issue comments and linked pull requests on GitHub "
            "to verify whether this issue is still relevant or open to community contribution."
        )

    part1_objective = {
        "title": "Issue Objective & Scope",
        "category": category,
        "state": state,
        "is_closed": is_closed,
        "closed_notice": closed_notice,
        "is_good_first_issue": evidence["is_good_first_issue"],
        "summary": objective_summary,
        "explanation": objective_explanation,
    }

    # Part 2: Pre-Coding Investigation
    investigation_steps = []
    if is_closed:
        investigation_steps.append(
            "Verify Closed Status & Rationale: This issue is marked as CLOSED. Check the closing comments, "
            "linked commits, or associated pull requests on GitHub to understand why it was closed and verify "
            "whether maintainers are still seeking contributions."
        )
    investigation_steps.append(
        "Review Maintainer Discussions: Check existing comments on the issue to verify if maintainers have provided implementation advice or if another contributor is already actively working on it."
    )

    # Evidence-aware investigation step
    if evidence["has_files"]:
        investigation_steps.append(
            f"Inspect Evidenced Source Files: Locate and review cited file(s) ({', '.join(evidence['files'][:3])}) to understand current behavior and surrounding logic."
        )
    elif evidence["symbols"]:
        investigation_steps.append(
            f"Trace Referenced Symbols: Search the repository for references to cited symbol(s) ({', '.join(evidence['symbols'][:3])}) to locate their definitions and callers."
        )
    else:
        investigation_steps.append(
            f"Repository Keyword Search: No source files were cited in the issue. Search the repository for keywords from the title (e.g., '{title[:40]}...') to find relevant modules."
        )
        investigation_steps.append(
            "Inspect Related Test Files: Search test directories (e.g. tests/, spec/) for existing tests covering related functionality to understand how the component is exercised."
        )

    # Category-specific investigation step
    if is_bug:
        investigation_steps.extend([
            "Locate Minimal Reproduction: Check if reproduction steps, sample inputs, or code snippets are provided in the issue description.",
            "Verify Failure Locally: Run the reproduction steps against the latest main branch to confirm the bug exists in current code.",
        ])
    elif is_feature:
        investigation_steps.extend([
            "Confirm Feature Alignment: Verify maintainers have accepted the proposal or assigned a milestone before writing substantial code.",
            "Review Existing Architecture: Check related commands, APIs, or classes in the repository to adhere to existing conventions.",
        ])
    elif is_doc:
        investigation_steps.extend([
            "Locate Relevant Documentation: Identify the exact markdown/rst files or docstrings that contain the outdated content.",
            "Verify Technical Accuracy: Check against current codebase behavior to ensure the updated docs reflect reality.",
        ])
    elif is_refactor:
        investigation_steps.extend([
            "Establish Test Baseline: Run the existing test suite covering the target component to ensure all tests pass before making refactoring changes.",
            "Verify Public Contracts: Check public APIs and interfaces to ensure the planned refactoring does not introduce breaking changes.",
        ])
    elif is_test:
        investigation_steps.extend([
            "Review Test Structure: Inspect repository test conventions, fixtures, and runners to ensure new tests conform to project standards.",
        ])

    part2_investigation = {
        "title": "Pre-Coding Investigation",
        "steps": investigation_steps,
    }

    # Part 3: Supported File & Component Evidence
    if evidence["has_files"]:
        evidence_notice = (
            "The following file paths and stack-trace locations were explicitly cited in the issue report. "
            "Inspect these files first as direct starting points:"
        )
    else:
        evidence_notice = (
            "No specific file paths or source files were cited in the issue evidence. "
            "Avoid guessing file locations without verifying. Recommended starting point: search the repository "
            f"for keywords from the title (e.g., '{title[:40]}...') or check related test suites and component directories."
        )

    part3_evidence = {
        "title": "Relevant Files & Component Evidence",
        "has_files": evidence["has_files"],
        "files": evidence["files"],
        "symbols": evidence["symbols"],
        "errors": evidence["errors"],
        "component_labels": evidence["component_labels"],
        "notice": evidence_notice,
    }

    # Part 4: Suggested Implementation Path
    if is_bug:
        branch_prefix = "fix"
    elif is_feature:
        branch_prefix = "feature"
    elif is_doc:
        branch_prefix = "docs"
    elif is_refactor:
        branch_prefix = "refactor"
    elif is_test:
        branch_prefix = "test"
    else:
        branch_prefix = "patch"

    implementation_milestones = [
        f"Create a dedicated feature branch: git checkout -b {branch_prefix}-issue-{issue_number} from the latest upstream main.",
    ]

    # Evidence-aware milestone
    if evidence["has_files"]:
        implementation_milestones.append(
            f"Focus edits on cited file(s): Begin implementation in {', '.join(evidence['files'][:3])}."
        )
    elif evidence["symbols"]:
        implementation_milestones.append(
            f"Locate and modify referenced symbol(s): Search for and update {', '.join(evidence['symbols'][:3])}."
        )
    else:
        implementation_milestones.append(
            "Search repository before editing: Because no source files were cited, locate relevant files by searching for related class or function names, and inspect corresponding unit tests."
        )

    # Category-specific implementation milestones
    if is_doc:
        implementation_milestones.extend([
            "Apply precise documentation edits, fixing grammar, terminology, and command examples.",
            "Preview generated documentation locally if the project uses a documentation builder (e.g. Sphinx, MkDocs, Docusaurus).",
        ])
    elif is_bug:
        implementation_milestones.extend([
            "Implement a minimal, targeted patch that corrects the defect without introducing side effects.",
            "Avoid broad refactoring or unrelated formatting edits in the same pull request.",
        ])
    elif is_feature:
        implementation_milestones.extend([
            "Implement the requested functionality incrementally following the project's established style.",
            "Ensure all newly added functions, parameters, or public APIs include clear docstrings and type annotations where customary.",
        ])
        if evidence.get("has_doc_signal"):
            implementation_milestones.append(
                "Update relevant documentation and docstrings to describe the new capability and configuration options."
            )
    elif is_refactor:
        implementation_milestones.extend([
            "Refactor code incrementally in small commits, verifying that all tests continue to pass after each step.",
            "Preserve existing behavior and ensure no regressions or unexpected side effects are introduced.",
        ])
    elif is_test:
        implementation_milestones.extend([
            "Add or improve automated test cases covering normal behavior, edge cases, and failure scenarios.",
            "Ensure tests are deterministic, avoid external network dependencies, and clean up temporary state.",
        ])
    else:
        implementation_milestones.extend([
            "Implement the requested improvement incrementally following repository conventions.",
            "Keep changes focused on the issue requirements without introducing unrelated modifications.",
        ])

    disclaimer = "Do not assume root cause or implementation details without testing against the codebase."
    if is_closed:
        disclaimer = (
            "Caution: This issue is closed on GitHub. Confirm with project maintainers before developing a patch "
            "or submitting a pull request, as the issue may already be resolved or not planned."
        )

    part4_implementation = {
        "title": "Suggested Implementation Path",
        "milestones": implementation_milestones,
        "disclaimer": disclaimer,
    }

    # Part 5: Testing & Verification Guidance
    if is_doc:
        test_step1 = "Verify Documentation Build: Build docs locally (e.g., make html or mkdocs build) and verify rendered HTML output and link validity."
    elif evidence["has_files"]:
        test_step1 = f"Write an Automated Test: Add a regression or unit test exercising changes in {evidence['files'][0]}."
    elif evidence["symbols"]:
        test_step1 = f"Write an Automated Test: Add a test covering the behavior of {evidence['symbols'][0]} under the scenario described in issue #{issue_number}."
    else:
        test_step1 = f"Write an Automated Test: Search existing test directories to find where related features are tested, and add a test case covering the scenario in issue #{issue_number}."

    testing_steps = [
        test_step1,
        "Run the Local Test Suite: Execute project tests to confirm existing functionality remains green without regressions.",
        "Run Linters and Formatters: Check code style using the repository's configured tools (e.g., flake8, ruff, black, eslint, prettier).",
    ]
    part5_testing = {
        "title": "Testing & Verification Guidance",
        "steps": testing_steps,
    }

    # Part 6: Pull Request Preparation
    if is_bug:
        commit_prefix = "fix"
    elif is_feature:
        commit_prefix = "feat"
    elif is_doc:
        commit_prefix = "docs"
    elif is_refactor:
        commit_prefix = "refactor"
    elif is_test:
        commit_prefix = "test"
    else:
        commit_prefix = "chore"

    pr_guidelines = [
        f"Write clear, atomic commit messages following repository conventions (e.g., '{commit_prefix}({name}): resolve {title[:30]}').",
        f"Reference this issue in the PR description using standard GitHub closing syntax: 'Fixes #{issue_number}' or 'Closes #{issue_number}'.",
        "Include a concise summary of the problem, the solution applied, and how you verified your changes in the PR description.",
        "Review repository CONTRIBUTING.md guidelines to confirm whether a changelog entry or signed contributor agreement (CLA) is required.",
    ]
    part6_pr = {
        "title": "Pull Request Preparation",
        "closing_keyword": f"Fixes #{issue_number}",
        "guidelines": pr_guidelines,
    }

    return {
        "objective": part1_objective,
        "investigation": part2_investigation,
        "evidence": part3_evidence,
        "implementation": part4_implementation,
        "testing": part5_testing,
        "pull_request": part6_pr,
    }


def analyze_issue_and_generate_plan(input_str: str, repo_context: dict | None = None) -> dict:
    """
    High-level orchestrator for Issue2Contribution:
    1. Validates and parses the issue URL or shorthand.
    2. Fetches issue details from the GitHub API.
    3. Handles pull request distinction.
    4. Generates an evidence-backed, six-part contribution plan.
    """
    owner, name, issue_number = parse_github_issue_url(input_str)
    issue_data = fetch_github_issue(owner, name, issue_number)

    if issue_data.get("is_pull_request"):
        # Explicit handling: item is a PR, not an issue
        plan = None
    else:
        plan = generate_contribution_plan(issue_data, repo_context=repo_context)

    return {
        "owner": owner,
        "name": name,
        "issue_number": issue_number,
        "issue": issue_data,
        "plan": plan,
    }
