import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request


spec = importlib.util.spec_from_file_location(
    "updater", Path(__file__).resolve().parents[1] / "scripts/update_repositories.py"
)
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


def metadata(stars=12, archived=False):
    return {"stargazers_count": stars, "archived": archived}


class RefreshTests(unittest.TestCase):
    def test_preserves_formatting_descriptions_age_and_zero_stars(self):
        original = "## Tools\r\n\r\n* **[Café ★ 100 ⧗ 17](https://github.com/Org/Repo/#details)** - Description.\r\n"
        expected = original.replace("★ 100", "★ 0")
        actual, changed, removed, checked = updater.refresh_readme(original, lambda _: metadata(0))
        self.assertEqual(actual, expected)
        self.assertEqual((changed, removed, checked), (1, [], 1))

    def test_unchanged_stars_are_noop(self):
        original = "* [App ★ 12](https://github.com/org/repo) - Description.\n"
        self.assertEqual(updater.refresh_readme(original, lambda _: metadata()), (original, 0, [], 1))

    def test_refreshed_lines_trim_single_blank_but_preserve_hard_breaks(self):
        prefix = "* [App ★ 1](https://github.com/org/repo) - Description."
        for ending in (" \n", "  \n", " \r\n", "  \r\n"):
            with self.subTest(ending=ending):
                expected_ending = ending[1:] if ending.startswith(" ") and not ending.startswith("  ") else ending
                text, _, _, _ = updater.refresh_readme(prefix + ending, lambda _: metadata(2))
                self.assertEqual(text, prefix.replace("★ 1", "★ 2") + expected_ending)

    def test_does_not_add_star_badges_to_new_entries(self):
        original = "* [App](https://github.com/org/repo) - Description.\n"
        self.assertEqual(updater.refresh_readme(original, lambda _: metadata()), (original, 0, [], 1))

    def test_removes_archived_and_unavailable_entries_without_badges(self):
        original = ("## Tools\n\n* [Old](https://github.com/org/old) - Description.\n"
                    "* [Gone](https://github.com/org/gone) - Description.\n"
                    "* [Site](https://example.com/) - Keep this.\n")
        fetch = lambda repo: metadata(archived=True) if repo.endswith("old") else None
        text, changed, removed, checked = updater.refresh_readme(original, fetch)
        self.assertEqual(text, "## Tools\n\n* [Site](https://example.com/) - Keep this.\n")
        self.assertEqual(changed, 0)
        self.assertEqual(removed, [("org/old", "archived"), ("org/gone", "unavailable (404/410)")])
        self.assertEqual(checked, 2)

    def test_queries_duplicates_once_case_insensitively(self):
        fetch = MagicMock(return_value=metadata(9))
        text = "* [One ★ 1](https://github.com/Org/Repo)\n* [Two ★ 2](https://github.com/org/repo/)\n"
        result, changed, _, checked = updater.refresh_readme(text, fetch)
        self.assertEqual(changed, 2)
        self.assertEqual(result.count("★ 9"), 2)
        self.assertEqual(checked, 1)
        fetch.assert_called_once_with("Org/Repo")

    def test_keeps_non_repository_links_inline_references_and_code_examples(self):
        original = ("* [Profile ★ 4](https://github.com/org)\n"
                    "* [File ★ 4](https://github.com/org/repo/tree/main)\n"
                    "* [Website ★ 4](https://example.com/org/repo)\n"
                    "* [Port ★ 4](https://github.com:443/org/repo)\n"
                    "See [Reference ★ 4](https://github.com/org/repo).\n"
                    "    * [Indented ★ 4](https://github.com/org/repo)\n"
                    "```markdown\n* [Example ★ 4](https://github.com/org/repo)\n```\n"
                    "~~~~\n* [Example ★ 4](https://github.com/org/repo)\n~~~~\n")
        fetch = MagicMock()
        self.assertEqual(updater.refresh_readme(original, fetch), (original, 0, [], 0))
        fetch.assert_not_called()

    def test_rejects_credential_host_and_path_traversal_urls(self):
        for url in ("https://github.com@evil.example/org/repo", "https://github.com/org/..",
                    "http://github.com/org/repo", "https://github.com/org/%2e%2e"):
            with self.subTest(url=url):
                self.assertIsNone(updater.repository_name(url))

    def test_api_failure_leaves_file_unchanged_even_after_planned_removal(self):
        original = "* [Gone](https://github.com/org/gone)\n* [Live ★ 1](https://github.com/org/live)\n"
        fetch = MagicMock(side_effect=[None, RuntimeError("GitHub returned HTTP 403")])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "README.md"
            path.write_text(original)
            with self.assertRaises(RuntimeError):
                updater.update_file(path, fetch)
            self.assertEqual(path.read_text(), original)
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_atomic_write_preserves_crlf_permissions_and_removes_temporary_file(self):
        original = b"* [App \xe2\x98\x85 1](https://github.com/org/repo)\r\n"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "README.md"
            path.write_bytes(original)
            path.chmod(0o644)
            updater.update_file(path, lambda _: metadata(2))
            self.assertEqual(path.read_bytes(), original.replace(b" 1]", b" 2]"))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_noop_does_not_replace_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "README.md"
            path.write_text("* [App ★ 12](https://github.com/org/repo)\n")
            with patch.object(updater.os, "replace") as replace:
                updater.update_file(path, lambda _: metadata())
            replace.assert_not_called()


class FetchTests(unittest.TestCase):
    def test_authentication_and_json_payload(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(metadata()).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(updater, "build_opener", return_value=opener), patch.dict(
                os.environ, {"GITHUB_TOKEN": "your-token-here"}):
            self.assertEqual(updater.fetch_repository("org/repo"), metadata())
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/org/repo")
        self.assertEqual(request.get_header("Authorization"), "Bearer your-token-here")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 20)

    def test_only_404_and_410_are_classified_unavailable(self):
        for code in (404, 410, 401, 403, 429, 500):
            opener = MagicMock()
            opener.open.side_effect = HTTPError("https://api.github.com/repos/org/repo", code,
                                               "request failed", {}, io.BytesIO())
            with self.subTest(code=code), patch.object(updater, "build_opener", return_value=opener):
                if code in (404, 410):
                    self.assertIsNone(updater.fetch_repository("org/repo"))
                else:
                    with self.assertRaises(RuntimeError):
                        updater.fetch_repository("org/repo")

    def test_transport_failures_abort(self):
        for error in (URLError("connection failed"), TimeoutError()):
            opener = MagicMock()
            opener.open.side_effect = error
            with self.subTest(error=error), patch.object(updater, "build_opener", return_value=opener):
                with self.assertRaises(RuntimeError):
                    updater.fetch_repository("org/repo")

    def test_invalid_metadata_aborts(self):
        for payload in ("not JSON", "[]", "{}", '{"stargazers_count": true, "archived": false}',
                        '{"stargazers_count": -1, "archived": false}',
                        '{"stargazers_count": 1, "archived": "false"}'):
            response = MagicMock()
            response.__enter__.return_value.read.return_value = payload.encode()
            opener = MagicMock()
            opener.open.return_value = response
            with self.subTest(payload=payload), patch.object(updater, "build_opener", return_value=opener):
                with self.assertRaises(RuntimeError):
                    updater.fetch_repository("org/repo")

    def test_redirects_cannot_forward_authentication_outside_github(self):
        handler = updater.GitHubRedirects()
        request = Request("https://api.github.com/repos/org/old", headers={"Authorization": "Bearer your-token-here"})
        for target in ("https://evil.example/", "http://api.github.com/repos/org/new",
                       "https://api.github.com@evil.example/"):
            with self.subTest(target=target), self.assertRaises(RuntimeError):
                handler.redirect_request(request, None, 301, "Moved", {}, target)
        redirected = handler.redirect_request(request, None, 301, "Moved", {},
                                               "https://api.github.com/repos/org/new")
        self.assertEqual(redirected.full_url, "https://api.github.com/repos/org/new")


if __name__ == "__main__":
    unittest.main()
