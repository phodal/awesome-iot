#!/usr/bin/env python3
"""Refresh existing stars and remove unavailable/archived GitHub list entries.

Only primary repository links in Markdown list entries are considered. Other
content, including activity-age values, is preserved. All requests finish before
README is changed; errors other than HTTP 404/410 abort the update.
"""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


ENTRY = re.compile(
    r"^ {0,3}[*+-]\s+(?:\*\*)?\[(?P<label>[^\]\r\n]+)\]"
    r"\((?P<url>https://github\.com/[^)\s]+)\)", re.IGNORECASE
)
STAR = re.compile(r"(★\s+)(\d+)")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


class GitHubRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        target = urlsplit(newurl)
        if target.scheme != "https" or target.netloc.lower() != "api.github.com":
            raise RuntimeError("Refusing an authenticated redirect outside GitHub's API")
        return super().redirect_request(request, response, code, message, headers, newurl)


def repository_name(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        return None
    match = re.fullmatch(r"/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+)/?", parsed.path)
    if not match or match[2] in (".", ".."):
        return None
    return "/".join(match.groups())


def fetch_repository(repository):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "awesome-iot-metadata"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request("https://api.github.com/repos/" + repository, headers=headers)
    try:
        with build_opener(GitHubRedirects()).open(request, timeout=20) as response:
            metadata = json.loads(response.read())
    except HTTPError as error:
        if error.code in (404, 410):
            return None
        raise RuntimeError("GitHub returned HTTP {} for {}".format(error.code, repository)) from None
    except (URLError, OSError, ValueError):
        raise RuntimeError("Could not read GitHub metadata for " + repository) from None
    if (not isinstance(metadata, dict)
            or type(metadata.get("stargazers_count")) is not int
            or metadata["stargazers_count"] < 0
            or type(metadata.get("archived")) is not bool):
        raise RuntimeError("Invalid GitHub repository metadata for " + repository)
    return metadata


def refresh_readme(text, fetch):
    cache, output, removed = {}, [], []
    refreshed = 0
    fence = None
    for line in text.splitlines(keepends=True):
        marker = FENCE.match(line)
        if fence:
            if (marker and marker[1][0] == fence[0]
                    and len(marker[1]) >= fence[1] and not line[marker.end():].strip()):
                fence = None
            output.append(line)
            continue
        if marker:
            fence = (marker[1][0], len(marker[1]))
            output.append(line)
            continue
        entry = ENTRY.match(line)
        repository = repository_name(entry["url"]) if entry else None
        if repository is None:
            output.append(line)
            continue
        key = repository.lower()
        if key not in cache:
            cache[key] = fetch(repository)
        metadata = cache[key]
        if metadata is None or metadata["archived"]:
            removed.append((repository, "unavailable (404/410)" if metadata is None else "archived"))
            continue
        label = STAR.sub(lambda match: match[1] + str(metadata["stargazers_count"]), entry["label"], count=1)
        if label != entry["label"]:
            line = line[:entry.start("label")] + label + line[entry.end("label"):]
            # Drop a single unused trailing blank, but preserve Markdown hard breaks.
            line = re.sub(r"(?<![ \t])[ \t](?=\r?\n?$)", "", line)
            refreshed += 1
        output.append(line)
    return "".join(output), refreshed, removed, len(cache)


def update_file(path, fetch=fetch_repository):
    path = Path(path)
    with path.open(encoding="utf-8", newline="") as source:
        original = source.read()
    updated, refreshed, removed, checked = refresh_readme(original, fetch)
    if updated != original:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                             dir=path.parent, delete=False) as destination:
                temporary = Path(destination.name)
                destination.write(updated)
            temporary.chmod(stat.S_IMODE(path.stat().st_mode))
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return refreshed, removed, checked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("readme", nargs="?", default="README.md")
    arguments = parser.parse_args()
    try:
        refreshed, removed, checked = update_file(arguments.readme)
    except (RuntimeError, OSError) as error:
        print("Update failed: " + str(error), file=sys.stderr)
        return 1
    print("Checked {} repositories; refreshed {} star counts; removed {} entries.".format(
        checked, refreshed, len(removed)))
    for repository, reason in removed:
        print("Removed {}: {}".format(repository, reason))
    return 0


if __name__ == "__main__":
    sys.exit(main())
