# RepoPilot

## Project Purpose
RepoPilot is an open-source platform designed to streamline and enhance the open-source contribution experience. It aims to bridge the gap between aspiring contributors and open-source projects by providing tools for repository discovery, code analysis, mentorship, and issue resolution guidance.

## Problem Statement
Navigating the vast landscape of open-source software (OSS) can be overwhelming for new developers. Finding suitable projects, understanding complex codebases, and knowing how to make meaningful contributions are significant barriers to entry. RepoPilot addresses these challenges by offering a centralized platform that guides developers through every step of their open-source journey.

## Planned Modules
*   **OSS Navigator**: A discovery tool to help developers find open-source projects that match their skills, interests, and experience levels.
*   **RepoPilot Repository Analysis**: Advanced tools to break down and analyze project codebases, making it easier to understand architecture, dependencies, and code flows.
*   **OSS Mentor**: A guidance system connecting developers with resources, best practices, and automated assistance to improve their coding skills in an open-source context.
*   **Issue2Contribution**: A focused workflow that assists developers in taking an open GitHub issue, understanding its requirements, and successfully creating a pull request.

## Current Status
*   **Implemented**: The foundational Django architecture and a basic home page have been set up. The database connection to PostgreSQL is configured securely via environment variables.
*   **Planned**: The core modules (OSS Navigator, Repository Analysis, OSS Mentor, and Issue2Contribution) are currently in the planning and development phase. 

## Technology Stack
*   **Backend**: Python 3.12, Django 6.1.2
*   **Database**: PostgreSQL 18.6 (psycopg)
*   **Frontend**: HTML/CSS

## Local Setup Instructions

1.  **Clone the repository**:
    ```bash
    git clone https://github.com/yourusername/RepoPilot.git
    cd RepoPilot
    ```

2.  **Set up a virtual environment** (recommended):
    ```bash
    python -m venv .venv
    # On Windows:
    .venv\Scripts\activate
    # On macOS/Linux:
    source .venv/bin/activate
    ```

3.  **Environment Variable Setup**:
    To securely manage credentials, we use environment variables.
    *   Copy the `.env.example` file to create a new file named `.env`:
        ```bash
        # On Windows (Command Prompt/PowerShell):
        copy .env.example .env
        # On macOS/Linux:
        cp .env.example .env
        ```
    *   Open the newly created `.env` file and replace the empty values with your actual local PostgreSQL database credentials:
        ```env
        DB_PASSWORD=your_actual_password_here
        ```

4.  **Run Migrations**:
    Ensure your local PostgreSQL database `oss_platform` is running and accessible.
    ```bash
    python manage.py migrate
    ```

5.  **Run the Development Server**:
    ```bash
    python manage.py runserver
    ```
    Access the application in your browser at `http://127.0.0.1:8000/`.

## Basic Contributor Workflow
1.  Fork the repository and clone your fork locally.
2.  Create a new branch for your feature or bug fix: `git checkout -b feature/your-feature-name`.
3.  Make your changes and ensure the project still runs correctly locally.
4.  Commit your changes with clear, descriptive commit messages.
5.  Push your branch to your fork: `git push origin feature/your-feature-name`.
6.  Open a Pull Request against the main repository.

## Future Roadmap
*   Phase 1: Complete Django foundation and user authentication.
*   Phase 2: Develop and integrate the OSS Navigator and RepoPilot Repository Analysis modules.
*   Phase 3: Roll out the OSS Mentor and Issue2Contribution workflows.
*   Phase 4: Community beta testing and feedback iteration.

## Contributors
*   [Contributor 1 Placeholder] - Core Developer
*   [Contributor 2 Placeholder] - Core Developer
