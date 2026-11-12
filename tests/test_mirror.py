import json
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.mirror import Build, normalize_download_url, parse_builds, parse_latest, planned_builds, promoted_beta_matches, prune_promoted_betas, publish, release_is_incomplete, suppressed_beta_tags, sync_changelogs, validate_download_url, with_changelog


class ParseLatestTests(unittest.TestCase):
    def release(self, tag, digest, *, complete=True, body=""):
        assets = [{"name": "Surge.zip", "state": "uploaded", "digest": f"sha256:{digest}"}]
        if complete:
            assets.append({"name": "SHA256SUMS.txt", "state": "uploaded"})
        return {"id": len(tag), "tag_name": tag, "draft": False, "body": body, "assets": assets}

    def test_selects_highest_build_not_first_item(self):
        xml = b'''<rss xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle"><channel>
        <item><enclosure url="https://dl.nssurge.com/mac/v6/Surge-old.zip"
          sparkle:version="100" sparkle:shortVersionString="6.1" /></item>
        <item><enclosure url="https://dl.nssurge.com/mac/v6/Surge-new.zip"
          sparkle:version="110" sparkle:shortVersionString="6.2" /></item>
        </channel></rss>'''
        build = parse_latest("beta", xml)
        self.assertEqual(build.tag, "surge-00000110-beta-v6.2")
        self.assertEqual(build.filename, "Surge-new.zip")

    def test_parses_official_markdown_changelog(self):
        xml = b'''<rss xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle"><channel>
        <item><markdownDescription><![CDATA[### Changes\n- Added SOCKS5 UDP.]]></markdownDescription>
        <enclosure url="https://dl.nssurge.com/mac/v6/Surge.zip"
          sparkle:version="12800" sparkle:shortVersionString="6.11.0" /></item>
        </channel></rss>'''

        build = parse_builds("rls", "feed", xml)[0]

        self.assertIn("Added SOCKS5 UDP", build.changelog)

    def test_changelog_replaces_existing_block_without_losing_promotion_marker(self):
        body = "Notes\n\n<!-- promoted-beta: surge-00012799-beta-v6.11.0 -->\n"
        markdown = "- Updated details.\n- [Download](https://dl.nssurge.com/mac/v6/Surge.zip)"

        updated = with_changelog(body, markdown)

        self.assertIn("<!-- promoted-beta:", updated)
        self.assertIn("- Updated details.", updated)
        self.assertNotIn("dl.nssurge.com", updated)
        self.assertEqual(with_changelog(updated, markdown), updated)
        self.assertIn("- Revised details.", with_changelog(updated, "- Revised details."))

    @patch("scripts.mirror.subprocess.run")
    @patch("scripts.mirror.subprocess.check_output")
    def test_sync_changelog_edits_only_release_body(self, check_output, run):
        build = Build("rls", "6.11.0", 12800, "https://dl.nssurge.com/mac/v6/Surge.zip",
                      "### Changes\n- Added SOCKS5 UDP.")
        release = self.release(build.tag, "a" * 64, body="Notes\n")
        check_output.return_value = json.dumps([release])

        self.assertEqual(sync_changelogs("LOVECHEN/surge-release", [build]), 1)

        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["gh", "api", "-X", "PATCH"])
        self.assertIn("Added SOCKS5 UDP", command[-1])
        self.assertNotIn("release", command[:3])

    def test_rejects_unexpected_download_host(self):
        with self.assertRaises(ValueError):
            validate_download_url("https://example.com/mac/v6/Surge.zip")

    def test_rejects_empty_feed(self):
        with self.assertRaisesRegex(ValueError, "No valid builds"):
            parse_latest("rls", b"<rss><channel /></rss>")

    def test_normalizes_old_v3_appcast_url(self):
        self.assertEqual(
            normalize_download_url("https://www.nssurge.com/mac/v3/Surge-example.zip"),
            "https://dl.nssurge.com/mac/v3/Surge-example.zip",
        )

    @patch("scripts.mirror.subprocess.check_output")
    def test_release_with_missing_zip_is_incomplete(self, check_output):
        build = Build("rls", "6.11.0", 12800, "https://dl.nssurge.com/mac/v6/Surge.zip")
        check_output.return_value = '["SHA256SUMS.txt"]'

        self.assertTrue(release_is_incomplete("LOVECHEN/surge-release", build))

    @patch("scripts.mirror.subprocess.check_output")
    def test_release_with_both_assets_is_complete(self, check_output):
        build = Build("rls", "6.11.0", 12800, "https://dl.nssurge.com/mac/v6/Surge.zip")
        check_output.return_value = '["SHA256SUMS.txt", "Surge.zip"]'

        self.assertFalse(release_is_incomplete("LOVECHEN/surge-release", build))

    def test_tags_sort_by_build_number(self):
        older = Build("rls", "6.9.1", 12290, "https://dl.nssurge.com/mac/v6/older.zip")
        stable = Build("rls", "6.11.0", 12800, "https://dl.nssurge.com/mac/v6/stable.zip")
        beta = Build("beta", "6.12.0", 12810, "https://dl.nssurge.com/mac/v6/beta.zip")

        self.assertEqual(sorted([older.tag, stable.tag, beta.tag], reverse=True),
                         [beta.tag, stable.tag, older.tag])

    @patch("scripts.mirror.subprocess.run")
    @patch("scripts.mirror.download", return_value="a" * 64)
    def test_beta_title_does_not_set_prerelease_flag(self, _download, run):
        beta = Build("beta", "6.12.0", 12810, "https://dl.nssurge.com/mac/v6/beta.zip")
        notes = []
        run.side_effect = lambda command, **_kwargs: notes.append(
            Path(command[command.index("--notes-file") + 1]).read_text(encoding="utf-8")
        )

        publish("LOVECHEN/surge-release", beta, "surge-00012800-rls-v6.11.0")

        command = run.call_args.args[0]
        self.assertIn("--latest=false", command)
        self.assertNotIn("--prerelease", command)
        self.assertIn("- SHA-256: `", notes[0])
        self.assertNotIn(beta.url, notes[0])

    @patch("scripts.mirror.fetch_builds")
    def test_beta_feed_stable_entries_are_not_beta_releases(self, fetch_builds):
        stable = Build("rls", "6.11.0", 12800, "https://dl.nssurge.com/mac/v6/stable.zip")
        duplicate = Build("beta", "6.11.0", 12800, stable.url)
        beta = Build("beta", "6.12.0", 12810, "https://dl.nssurge.com/mac/v6/beta.zip")
        fetch_builds.side_effect = [[stable], [beta, duplicate]]

        builds, latest_tag = planned_builds("all", False)

        self.assertEqual([build.tag for build in builds], [stable.tag, beta.tag])
        self.assertEqual(latest_tag, stable.tag)

    def test_promoted_beta_requires_complete_stable_with_matching_zip_sha(self):
        stable = self.release("surge-00012800-rls-v6.11.0", "a" * 64)
        promoted = self.release("surge-00012799-beta-v6.11.0", "a" * 64)
        other = self.release("surge-00012810-beta-v6.12.0", "b" * 64)
        incomplete = self.release("surge-00012820-rls-v6.12.0", "b" * 64, complete=False)

        self.assertEqual(promoted_beta_matches([stable, promoted, other, incomplete]), [(stable, promoted)])

    @patch("scripts.mirror.subprocess.run")
    @patch("scripts.mirror.subprocess.check_output")
    def test_prune_records_beta_before_deleting_release_and_tag(self, check_output, run):
        stable = self.release("surge-00012800-rls-v6.11.0", "a" * 64)
        beta = self.release("surge-00012799-beta-v6.11.0", "a" * 64)
        check_output.return_value = json.dumps([stable, beta])

        self.assertEqual(prune_promoted_betas("LOVECHEN/surge-release"), 1)

        patch_command = run.call_args_list[0].args[0]
        delete_command = run.call_args_list[1].args[0]
        self.assertIn("-X", patch_command)
        self.assertIn("PATCH", patch_command)
        self.assertIn("<!-- promoted-beta: surge-00012799-beta-v6.11.0 -->", patch_command[-1])
        self.assertEqual(delete_command[:4], ["gh", "release", "delete", beta["tag_name"]])
        self.assertIn("--cleanup-tag", delete_command)
        updated_body = patch_command[-1].removeprefix("body=")
        self.assertEqual(suppressed_beta_tags([{"tag_name": stable["tag_name"], "body": updated_body}]),
                         {beta["tag_name"]})

    @patch("scripts.mirror.subprocess.run")
    @patch("scripts.mirror.subprocess.check_output")
    def test_prune_with_existing_marker_does_not_patch_again(self, check_output, run):
        beta_tag = "surge-00012799-beta-v6.11.0"
        stable = self.release("surge-00012800-rls-v6.11.0", "a" * 64,
                              body=f"Notes\n\n<!-- promoted-beta: {beta_tag} -->\n")
        beta = self.release(beta_tag, "a" * 64)
        check_output.return_value = json.dumps([stable, beta])

        self.assertEqual(prune_promoted_betas("LOVECHEN/surge-release"), 1)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][1:3], ["release", "delete"])


if __name__ == "__main__":
    unittest.main()
