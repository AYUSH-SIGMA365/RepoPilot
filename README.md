# RepoPilot

[![Python](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![Django](https://img.shields.io/badge/Django-6.1-green.svg)](https://www.djangoproject.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-18-blue.svg)](https://www.postgresql.org/)
[![Bootstrap](https://img.shields.io/badge/Bootstrap-5.3.3-purple.svg)](https://getbootstrap.com/)
[![Tests](https://img.shields.io/badge/Tests-147%20Passed-brightgreen.svg)]()

**RepoPilot** is an open-source platform designed to streamline and elevate the open-source contribution experience. It bridges the gap between aspiring contributors and open-source projects by providing tools for repository discovery, health and readiness analysis, interactive onboarding mentorship, and evidence-grounded issue resolution planning.

---

## Table of Contents

- [Core Modules](#core-modules)
  - [1. Repository Analysis & Readiness Scoring](#1-repository-analysis--readiness-scoring)
  - [2. OSS Navigator](#2-oss-navigator)
  - [3. OSS Mentor](#3-oss-mentor)
  - [4. Issue2Contribution](#4-issue2contribution)
- [Technology Stack](#technology-stack)
- [Architecture & Design Principles](#architecture--design-principles)
- [Local Setup Instructions](#local-setup-instructions)
- [Running Tests & Quality Checks](#running-tests--quality-checks)
- [Project Directory Structure](#project-directory-structure)
- [Contributor Workflow](#contributor-workflow)
- [License](#license)

---

## Core Modules

### 1. Repository Analysis & Readiness Scoring
*Route: `/`*

Evaluates any public GitHub repository and computes a deterministic **100-Point Readiness Score**:
- **Repository Health Metrics**: Real-time retrieval of stars, forks, open issues, license, contributor counts, and recent commit activity.
- **Deterministic 100-Point Breakdown**:
  - *Documentation & Standards (40 pts)*: Presence and quality of `README`, `CONTRIBUTING.md`, `LICENSE`, and `CODE_OF_CONDUCT.md`.
  - *Community & Contributor Base (20 pts)*: Active contributor diversity and community health.
  - *Commit Activity & Recency (20 pts)*: Scoring based on recent push timestamps and commit cadence.
  - *Issue Velocity & Responsiveness (20 pts)*: Ratio of resolved issues to open backlog.
- **Persistence**: Analyzed repositories are cached and updated in PostgreSQL (`oss_platform`).

### 2. OSS Navigator
*Route: `/navigator/`*

A discovery engine helping developers find open-source repositories matching their skills and interests:
- **Curated Repository Catalog**: Filter by primary programming language (Python, JavaScript, TypeScript, Go, Rust, etc.), ecosystem topics, and readiness tiers.
- **Dynamic Sorting & Filtering**: Sort by readiness score, stars, forks, or recent activity.
- **Pre-computed Readiness Badges**: Quickly identify beginner-friendly repositories with established contribution standards.

### 3. OSS Mentor
*Route: `/mentor/`*

An interactive, repository-specific onboarding guide for new contributors:
- **Interactive Checklist**: Step-by-step contribution milestones (forking, local environment setup, reproduction, test suite execution, conventional commits, pull request submission).
- **Progress Tracking**: Contributor checklist progress is persisted in PostgreSQL per repository.
- **Accurate Issue-Count Pipeline**: Uses GitHub's issue-only Search API to report verified open issue counts, separating PRs from issues.
- **External Issue Tracker Detection**: Seamlessly detects when repositories (e.g. Django, Linux, GCC) use external trackers such as Trac, Bugzilla, or Jira, displaying external links rather than misleading zero counts.

### 4. Issue2Contribution
*Route: `/issue/`*

Transforms public GitHub issues into structured, actionable contribution plans:
- **Safe Input Parsing**: Accepts full GitHub issue URLs or `owner/repo#number` shorthand formats. Validates schemes (HTTP/HTTPS only), hostnames, and positive integer issue IDs; rejects PR URLs early.
- **Deterministic Evidence Extraction**: Uses regex and syntactic parsing to extract:
  - Stack trace frames (Python, JavaScript/TypeScript/Node).
  - Unified diff headers (`+++ b/path/to/file`).
  - Explicitly referenced code files, function/class symbols, and error signatures.
  - Filters out asset attachments (`.png`, `.svg`), version numbers, and dotfiles without hallucinating unevidenced files.
- **Multi-Signal Category Classifier**: Deterministically scores and categorizes issues across labels, titles, and issue template markers into:
  - `Feature Request / Enhancement`
  - `Bug Fix`
  - `Documentation Improvement`
  - `Refactoring / Cleanup`
  - `Test Suite Improvement`
  - `General Improvement` (fallback)
- **Structured 6-Part Contribution Plan**:
  1. *Objective & Scope*: Summary, category badge, and scope boundary guidelines.
  2. *Pre-Coding Investigation*: Evidence-aware investigation steps (cites evidenced files/symbols; recommends repository search and test inspection when evidence is absent).
  3. *Relevant Files & Component Evidence*: Cited files with one-click copy buttons, symbols, exception signatures, and zero-evidence notices.
  4. *Suggested Implementation Path*: Dedicated branch command (`feature-`, `fix-`, `docs-`, etc.) and implementation milestones (avoids documentation-only steps for feature requests).
  5. *Testing & Verification Guidance*: Step-by-step verification plan with local test suite and linter recommendations.
  6. *Pull Request Preparation*: Recommended conventional commit message, GitHub closing keyword box (`Fixes #<number>`) with copy helper, and PR best practices.

---

## Technology Stack

- **Backend**: Python 3.12, Django 6.1.2
- **Database**: PostgreSQL 18 (production/development via `psycopg 3.2`), SQLite (in-memory for isolated test runner)
- **Frontend**: Standalone HTML5 templates styled with Bootstrap 5.3.3 and Bootstrap Icons 1.11.3
- **External Integration**: GitHub REST API v3 (rate-limit aware with exponential backoff)
- **Deterministic Engine**: 100% rule-based and evidence-grounded; no external LLM hallucination or AI service dependencies

---

## Architecture & Design Principles

1. **Zero Hallucination Guarantee**: All issue analysis, file references, and readiness scores are computed strictly from verified API data and source text.
2. **Safe API Interaction**: Never calls user-supplied URLs directly. All GitHub API endpoints are constructed internally with canonical validation.
3. **Resilient Error Disclosure**: Internal exception tracebacks, authentication tokens, and upstream HTTP status codes (e.g. status 500) are never leaked to user interfaces; diagnostics are preserved in server logs.
4. **Accessible UI**: Keyboard accessible controls (`Tab`, `Space`, `Enter`), semantic headings, CSRF protection, and clipboard fallbacks via standard DOM APIs.

---

## Local Setup Instructions

### Prerequisites
- Python 3.12+
- PostgreSQL 18+ (running locally with database `oss_platform`)
- Git

### 1. Clone the repository
```bash
git clone https://github.com/yourusername/RepoPilot.git
cd RepoPilot
```

### 2. Set up a virtual environment
```bash
# Windows
python -m venv .venv
.venv\Scripts\activate

# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies
```bash
pip install -r requirements.txt
```

### 4. Configure environment variables
Create a `.env` file in the project root based on `.env.example`:
```bash
# Windows (PowerShell)
copy .env.example .env

# macOS / Linux
cp .env.example .env
```

Configure your local database password and optional GitHub personal access token (recommended to raise API limits from 60 to 5,000 requests/hour):
```env
DB_PASSWORD=your_postgres_password
GITHUB_TOKEN=your_github_personal_access_token_optional
```

### 5. Run database migrations
```bash
python manage.py migrate
```

### 6. Start the development server
```bash
python manage.py runserver
```

Open your browser at `http://127.0.0.1:8000/`.

---

## Running Tests & Quality Checks

RepoPilot includes an automated test suite of **147 unit and integration tests** covering all service layers, controllers, view rendering, edge cases, and rate-limit handling:

```bash
# Run the full test suite
python manage.py test

# Run Django system integrity check
python manage.py check

# Check git diff formatting and whitespace
git diff --check
```

*Note: The test suite runs against an isolated in-memory SQLite database and uses mocked GitHub client responses, requiring zero network calls or credentials.*

---

## Project Directory Structure

```text
RepoPilot/
├── config/                  # Django project configuration
│   ├── settings.py          # Settings, database configuration, auth
│   ├── urls.py              # Root URL routing
│   └── wsgi.py              # WSGI entrypoint
├── core/                    # Core application
│   ├── checklist.py         # OSS Mentor onboarding checklist logic
│   ├── github_service.py    # GitHub API client, readiness scoring, repository service
│   ├── issue_service.py     # Issue2Contribution parsing, classification, plan engine
│   ├── models.py            # AnalyzedRepository, OnboardingProgress models
│   ├── templates/core/      # Standalone Bootstrap 5 templates
│   │   ├── home.html        # Repository Analysis & Readiness Scoring
│   │   ├── navigator.html   # OSS Navigator repository discovery
│   │   ├── mentor.html      # OSS Mentor checklist & onboarding guide
│   │   └── issue2contribution.html # Issue2Contribution plan generator
│   ├── tests.py             # 147 unit and integration tests
│   ├── urls.py              # Core application route definitions
│   └── views.py             # View controllers and request handlers
├── manage.py                # Django management script
├── requirements.txt         # Project Python dependencies
└── README.md                # Project documentation
```

---

## Contributor Workflow

1. Fork the repository and clone your fork locally.
2. Create a feature branch: `git checkout -b feature/your-feature-name`.
3. Implement your changes following project architectural conventions.
4. Verify the test suite and quality gates:
   ```bash
   python manage.py check
   python manage.py test
   git diff --check
   ```
5. Commit using conventional commit format: `feat(scope): concise description`.
6. Push to your fork and submit a Pull Request.

---

## License

This project is licensed under the [MIT License](LICENSE).
