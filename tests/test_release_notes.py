import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.mirror import Build, publish, sync_changelogs


class ReleaseNotesTests(unittest.TestCase):
    @patch("scripts.mirror.subprocess.run")
    @patch("scripts.mirror.download", return_value="a" * 64)
    def test_new_release_omits_redundant_lines(self, _download, run):
        build = Build("rls", "6.12.0", 12840, "https://dl.nssurge.com/mac/v6/Surge.zip")
        notes = []
        run.side_effect = lambda command, **_kwargs: notes.append(
            Path(command[command.index("--notes-file") + 1]).read_text(encoding="utf-8")
        )

        publish("LOVECHEN/surge-release", build, build.tag)

        self.assertIn("- SHA-256: `", notes[0])
        self.assertNotIn("Official, unmodified ZIP.", notes[0])
        self.assertNotIn("- Channel:", notes[0])

    @patch("scripts.mirror.subprocess.run")
    @patch("scripts.mirror.list_releases")
    def test_sync_cleans_all_mirror_releases_without_changing_other_notes(self, list_releases, run):
        build = Build("rls", "6.12.0", 12840, "https://dl.nssurge.com/mac/v6/Surge.zip")
        old_body = (
            "## Surge for Mac 6.9.0 (12250) · Release\n\n"
            "Official, unmodified ZIP.\n\n"
            "- Channel: `rls`\n"
            "- SHA-256: `abc`\n\n"
            "<!-- surge-changelog:start -->\n### Changelog\n\nFixed a bug.\n"
            "<!-- surge-changelog:end -->\n\n"
            "<!-- promoted-beta: surge-00012249-beta-v6.9.0 -->\n"
        )
        list_releases.return_value = [
            {"id": 1, "tag_name": "surge-00012250-rls-v6.9.0", "body": old_body},
            {"id": 2, "tag_name": "other-project", "body": old_body},
        ]

        self.assertEqual(sync_changelogs("LOVECHEN/surge-release", [build]), 1)

        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["gh", "api", "-X", "PATCH"])
        self.assertTrue(command[4].endswith("/releases/1"))
        self.assertIn("- SHA-256: `abc`", command[-1])
        self.assertIn("Fixed a bug.", command[-1])
        self.assertIn("<!-- promoted-beta:", command[-1])
        self.assertNotIn("Official, unmodified ZIP.", command[-1])
        self.assertNotIn("- Channel:", command[-1])


if __name__ == "__main__":
    unittest.main()
