import html
import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from typing import Any

ROOT = pathlib.Path(__file__).parent
USERNAME = "arijitroy003"
MAX_RETRY_DELAY_SECONDS = 60
REPO_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

FEATURED_REPOS = (
    "linkedin-mcp-server",
    "snap-a-miro",
    "datadiff",
    "flight-tracker",
)

ACTIVITY_LABELS = {
    "PushEvent": "Pushed commits to",
    "CreateEvent": "Created a repository, branch, or tag in",
    "PullRequestEvent": "Worked on a pull request in",
    "IssuesEvent": "Worked on an issue in",
    "ReleaseEvent": "Published a release in",
}

ORGANIZATION_LABELS = {
    "RedHatOfficial": "Red Hat",
    "apache": "Apache",
    "dbt-labs": "dbt Labs",
    "duckdb": "DuckDB",
    "langchain-ai": "LangChain",
    "llm-d": "llm-d",
    "redhat-data-and-ai": "Red Hat",
    "vllm-project": "vLLM",
}


class GitHubAPIError(RuntimeError):
    """Raised when public profile data cannot be fetched from GitHub."""


def github_url(path: str, **params: Any) -> str:
    query = urllib.parse.urlencode(params)
    return (
        f"https://api.github.com{path}?{query}"
        if query
        else f"https://api.github.com{path}"
    )


def fetch_json(url: str) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{USERNAME}-profile-readme",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    for attempt in range(3):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace").casefold()
            retry_after = exc.headers.get("Retry-After")
            secondary_limited = exc.code == 403 and (
                retry_after is not None or "secondary rate limit" in error_body
            )
            rate_limited = (
                exc.code == 429
                or secondary_limited
                or (exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0")
            )
            retryable = rate_limited or exc.code in {429, 500, 502, 503, 504}
            if retryable and attempt < 2:
                try:
                    if retry_after:
                        delay = float(retry_after)
                    elif exc.headers.get("X-RateLimit-Reset") and rate_limited:
                        delay = float(exc.headers["X-RateLimit-Reset"]) - time.time()
                    elif rate_limited:
                        delay = MAX_RETRY_DELAY_SECONDS
                    else:
                        delay = 2**attempt
                except ValueError:
                    delay = 2**attempt
                if delay > MAX_RETRY_DELAY_SECONDS:
                    raise GitHubAPIError(
                        f"GitHub requested a {delay:.0f}-second retry delay for {url}"
                    ) from exc
                time.sleep(max(delay, 1))
                continue
            raise GitHubAPIError(f"GitHub returned HTTP {exc.code} for {url}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < 2:
                time.sleep(2**attempt)
                continue
            raise GitHubAPIError(f"Could not reach GitHub for {url}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise GitHubAPIError(f"GitHub returned invalid JSON for {url}") from exc

    raise GitHubAPIError(f"Could not fetch {url}")


def marker_bounds(content: str, marker: str) -> tuple[int, int]:
    start_marker = f"<!-- {marker} starts -->"
    end_marker = f"<!-- {marker} ends -->"
    if content.count(start_marker) != 1 or content.count(end_marker) != 1:
        raise ValueError(f"Expected exactly one '{marker}' marker pair in README.md")

    start_index = content.index(start_marker)
    end_index = content.index(end_marker)
    if start_index >= end_index:
        raise ValueError(f"Invalid '{marker}' marker order in README.md")

    return start_index, end_index + len(end_marker)


def validate_marker_layout(content: str, markers: list[str]) -> None:
    ranges = sorted(
        (start, end, marker)
        for marker in markers
        for start, end in [marker_bounds(content, marker)]
    )
    for (_, previous_end, previous_marker), (
        current_start,
        _,
        current_marker,
    ) in zip(ranges, ranges[1:]):
        if current_start < previous_end:
            raise ValueError(
                f"README markers '{previous_marker}' and '{current_marker}' overlap"
            )


def replace_chunk(content: str, marker: str, chunk: str) -> str:
    start_marker = f"<!-- {marker} starts -->"
    end_marker = f"<!-- {marker} ends -->"
    start_index, end_index = marker_bounds(content, marker)
    replacement = f"{start_marker}\n{chunk}\n{end_marker}"
    return content[:start_index] + replacement + content[end_index:]


def markdown_text(value: Any) -> str:
    text = " ".join(str(value).split())
    text = html.escape(text, quote=False)
    for character in "\\`*_[]{}()#!|>~":
        text = text.replace(character, f"\\{character}")
    text = re.sub(r"(?i)\b((?:https?|ftp):)(?=//)", r"\1&#8203;", text)
    text = re.sub(r"(?i)\b(www)(?=\.)", r"\1&#8203;", text)
    text = re.sub(r"@(?=[A-Za-z0-9_.-]+\.[A-Za-z]{2,})", "@&#8203;", text)
    return text


def human_join(values: list[str]) -> str:
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    return f"{', '.join(values[:-1])}, and {values[-1]}"


def fetch_featured_repos() -> list[dict[str, Any]]:
    repos = []
    for name in FEATURED_REPOS:
        repo = fetch_json(github_url(f"/repos/{USERNAME}/{name}"))
        if (
            not isinstance(repo, dict)
            or repo.get("name") != name
            or repo.get("fork") is not False
            or not isinstance(repo.get("description"), (str, type(None)))
            or not isinstance(repo.get("language"), (str, type(None)))
        ):
            raise GitHubAPIError(f"GitHub returned invalid metadata for {name}")
        repos.append(repo)
    return repos


def render_projects(repos: list[dict[str, Any]]) -> str:
    if not repos:
        return "_No featured public projects are available right now._"

    lines = [
        "| Project | What it explores | Language |",
        "| --- | --- | --- |",
    ]
    for repo in repos:
        raw_name = repo.get("name", "Untitled")
        if not isinstance(raw_name, str) or not REPO_NAME_PATTERN.fullmatch(raw_name):
            continue
        name = markdown_text(raw_name)
        url = f"https://github.com/{USERNAME}/{raw_name}"
        description = markdown_text(repo.get("description") or "Public experiment")
        language = markdown_text(repo.get("language") or "Mixed")
        lines.append(f"| [**{name}**]({url}) | {description} | `{language}` |")
    return "\n".join(lines)


def fetch_recent_activity() -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    seen_repos: set[str] = set()
    for page in range(1, 4):
        url = github_url(
            f"/users/{USERNAME}/events/public",
            per_page=100,
            page=page,
        )
        events = fetch_json(url)
        if not isinstance(events, list):
            raise GitHubAPIError("GitHub activity response was not a list")

        for event in events:
            if not isinstance(event, dict):
                raise GitHubAPIError("GitHub activity contained an invalid event")
            event_type = event.get("type")
            if not isinstance(event_type, str):
                raise GitHubAPIError("GitHub activity event type was invalid")
            if event_type not in ACTIVITY_LABELS:
                continue

            repo_data = event.get("repo")
            created_at = event.get("created_at")
            if (
                not isinstance(repo_data, dict)
                or not isinstance(repo_data.get("name"), str)
                or not isinstance(created_at, str)
                or len(created_at) < 10
            ):
                raise GitHubAPIError("GitHub activity event fields were invalid")

            repo = repo_data["name"]
            if not REPOSITORY_PATTERN.fullmatch(repo):
                raise GitHubAPIError("GitHub activity repository name was invalid")
            if repo in seen_repos:
                continue

            seen_repos.add(repo)
            entries.append(
                {
                    "action": ACTIVITY_LABELS[event_type],
                    "repo": repo,
                    "url": f"https://github.com/{repo}",
                    "date": created_at[:10],
                }
            )
            if len(entries) == 5:
                return entries

        if len(events) < 100:
            break
    return entries


def render_activity(entries: list[dict[str, str]]) -> str:
    if not entries:
        return "_No recent public events are available._"

    return "\n".join(
        f"- {markdown_text(entry['action'])} "
        f"[**{markdown_text(entry['repo'])}**]({entry['url']})"
        f" · `{entry['date']}`"
        for entry in entries
    )


def fetch_open_source_prs() -> list[dict[str, Any]]:
    pull_requests: list[dict[str, Any]] = []
    for page in range(1, 11):
        url = github_url(
            "/search/issues",
            q=f"type:pr author:{USERNAME} is:merged is:public",
            per_page=100,
            page=page,
            sort="updated",
            order="desc",
        )
        response = fetch_json(url)
        if not isinstance(response, dict):
            raise GitHubAPIError("GitHub pull request search response was invalid")
        if response.get("incomplete_results") is not False:
            raise GitHubAPIError("GitHub pull request search results were incomplete")

        total_count = response.get("total_count")
        items = response.get("items")
        if not isinstance(total_count, int) or not isinstance(items, list):
            raise GitHubAPIError("GitHub pull request search fields were invalid")
        if total_count > 1000:
            raise GitHubAPIError(
                "GitHub pull request search exceeded its 1,000-result limit"
            )

        pull_requests.extend(items)
        if len(pull_requests) >= total_count or len(items) < 100:
            break

    external_prs: list[dict[str, Any]] = []
    seen_pull_requests: set[Any] = set()
    for pull_request in pull_requests:
        if not isinstance(pull_request, dict):
            raise GitHubAPIError("GitHub pull request search contained an invalid item")

        repository_url = pull_request.get("repository_url")
        number = pull_request.get("number")
        pull_request_id = pull_request.get("id")
        title = pull_request.get("title")
        closed_at = pull_request.get("closed_at")
        if (
            not isinstance(repository_url, str)
            or "/repos/" not in repository_url
            or not isinstance(number, int)
            or not isinstance(pull_request_id, int)
            or not isinstance(title, str)
            or not isinstance(closed_at, str)
        ):
            raise GitHubAPIError("GitHub pull request fields were invalid")

        repository = repository_url.split("/repos/", 1)[1]
        if not REPOSITORY_PATTERN.fullmatch(repository):
            raise GitHubAPIError("GitHub pull request repository name was invalid")
        if repository.split("/", 1)[0].casefold() == USERNAME.casefold():
            continue

        if pull_request_id in seen_pull_requests:
            continue
        seen_pull_requests.add(pull_request_id)
        external_prs.append({**pull_request, "repository": repository})

    external_prs.sort(
        key=lambda pull_request: (
            pull_request["repository"].casefold(),
            pull_request["number"],
        )
    )
    external_prs.sort(
        key=lambda pull_request: pull_request.get("closed_at") or "",
        reverse=True,
    )
    return external_prs


def render_open_source(pull_requests: list[dict[str, Any]]) -> str:
    if not pull_requests:
        return "_No merged upstream pull requests are available right now._"

    repositories = {pull_request["repository"] for pull_request in pull_requests}
    organization_counts = Counter(
        ORGANIZATION_LABELS.get(
            pull_request["repository"].split("/", 1)[0],
            pull_request["repository"].split("/", 1)[0],
        )
        for pull_request in pull_requests
    )
    communities = sorted(
        organization_counts,
        key=lambda organization: (
            -organization_counts[organization],
            organization.casefold(),
        ),
    )

    lines = [
        f"**{len(pull_requests)} merged upstream pull requests** across "
        f"**{len(repositories)} repositories**, including the "
        f"{human_join(communities)} communities.",
        "",
        "<details>",
        "<summary><strong>Latest merged upstream pull requests</strong></summary>",
        "",
    ]
    for pull_request in pull_requests[:6]:
        repository = markdown_text(pull_request["repository"])
        number = pull_request.get("number", "")
        title = markdown_text(pull_request.get("title", "Merged contribution")[:180])
        url = f"https://github.com/{pull_request['repository']}/pull/{number}"
        merged_date = (pull_request.get("closed_at") or "")[:10]
        lines.append(
            f"- [**{repository} #{number}**]({url}) — {title} · `{merged_date}`"
        )

    lines.extend(["", "</details>"])
    return "\n".join(lines)


def main() -> None:
    readme_path = ROOT / "README.md"
    original = readme_path.read_text(encoding="utf-8")
    readme = original
    refreshed: list[str] = []
    failures: list[str] = []

    sections = (
        ("projects", fetch_featured_repos, render_projects),
        ("activity", fetch_recent_activity, render_activity),
        ("open-source", fetch_open_source_prs, render_open_source),
    )
    validate_marker_layout(original, [marker for marker, _, _ in sections])
    for marker, fetcher, renderer in sections:
        try:
            data = fetcher()
        except GitHubAPIError as exc:
            failures.append(f"{marker}: {exc}")
            continue

        readme = replace_chunk(readme, marker, renderer(data))
        refreshed.append(marker)

    if failures:
        for failure in failures:
            print(f"Error: preserving README because {failure}")
        raise SystemExit(1)

    if readme != original:
        readme_path.write_text(readme, encoding="utf-8")

    status = ", ".join(refreshed) if refreshed else "no sections"
    print(f"README refresh complete: {status} updated")


if __name__ == "__main__":
    main()
