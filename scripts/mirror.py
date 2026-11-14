#!/usr/bin/env python3
"""Mirror official Surge for Mac builds to GitHub Releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path


FEEDS = {
    "rls": os.environ.get("SURGE_RLS_APPCAST", ""),
    "beta": os.environ.get("SURGE_BETA_APPCAST", ""),
}
ARCHIVE_FEEDS = tuple(
    url.strip()
    for url in os.environ.get("SURGE_ARCHIVE_APPCASTS", "").splitlines()
    if url.strip()
)
LEGACY_ZIP = os.environ.get("SURGE_LEGACY_ZIP", "")
SPARKLE = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"
MAX_ZIP_BYTES = 2_000_000_000
PROMOTED_BETA = re.compile(r"<!-- promoted-beta: (surge-[0-9]{8}-beta-v[A-Za-z0-9._-]+) -->")
SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
SOURCE_ADDRESS = re.compile(r"https?://(?:dl\.|www\.)?nssurge\.com/mac/[^\s)<>]+", re.IGNORECASE)
CHANGELOG_BLOCK = re.compile(r"<!-- surge-changelog:start -->.*?<!-- surge-changelog:end -->", re.DOTALL)
RELEASE_BOILERPLATE = re.compile(r"(?m)^Official, unmodified ZIP\.\n\n- Channel: `(rls|beta)`\n")
LEGACY_CHANGELOG = "- Fixed a compatibility issue with 304 responses.\n- Fixed a Dashboard crash."


@dataclass(frozen=True)
class Build:
    channel: str
    version: str
    number: int
    url: str
    changelog: str = ""

    @property
    def tag(self) -> str:
        return f"surge-{self.number:08d}-{self.channel}-v{self.version}"

    @property
    def filename(self) -> str:
        return Path(urllib.parse.urlparse(self.url).path).name


def validate_download_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "dl.nssurge.com"
        or parsed.port is not None
        or not parsed.path.startswith("/mac/")
        or not parsed.path.endswith(".zip")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Unexpected official download URL")


def normalize_download_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https" and parsed.hostname == "www.nssurge.com" and parsed.path.startswith("/mac/v3/"):
        url = urllib.parse.urlunparse(parsed._replace(netloc="dl.nssurge.com"))
    validate_download_url(url)
    return url


def parse_builds(channel: str, feed_url: str, data: bytes) -> list[Build]:
    root = ET.fromstring(data)
    builds: list[Build] = []
    for item in root.findall("./channel/item"):
        enclosure = item.find("enclosure")
        if enclosure is None:
            continue
        version = enclosure.get(f"{SPARKLE}shortVersionString", "")
        number = enclosure.get(f"{SPARKLE}version", "")
        url = enclosure.get("url", "")
        if not version or not number.isdecimal() or not url:
            continue
        builds.append(Build(channel, version, int(number), normalize_download_url(url),
                            item.findtext("markdownDescription") or ""))
    if not builds:
        raise ValueError(f"No valid builds in {feed_url}")
    return builds


def parse_latest(channel: str, data: bytes) -> Build:
    return max(parse_builds(channel, FEEDS[channel], data), key=lambda build: build.number)


def fetch_builds(channel: str, feed_url: str) -> list[Build]:
    request = urllib.request.Request(feed_url, headers={"User-Agent": "surge-release-mirror/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return parse_builds(channel, feed_url, response.read())


def validate_sources(backfill: bool) -> None:
    urls = [*FEEDS.values(), *(ARCHIVE_FEEDS if backfill else ())]
    if any(not url for url in urls) or (backfill and (not ARCHIVE_FEEDS or not LEGACY_ZIP)):
        raise ValueError("Required source secrets are not configured")
    for url in urls:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {"nssurge.com", "www.nssurge.com"} or not parsed.path.endswith(".xml"):
            raise ValueError("Invalid appcast secret value")
    if backfill:
        validate_download_url(LEGACY_ZIP)


def list_releases(repo: str) -> list[dict]:
    releases: list[dict] = []
    page = 1
    while True:
        result = subprocess.check_output(
            ["gh", "api", f"repos/{repo}/releases?per_page=100&page={page}"],
            text=True,
        )
        batch = json.loads(result)
        releases.extend(batch)
        if len(batch) < 100:
            return releases
        page += 1


def uploaded_zip_digest(release: dict) -> str | None:
    if release.get("draft"):
        return None
    assets = [asset for asset in release.get("assets", []) if asset.get("state") == "uploaded"]
    if not any(asset.get("name") == "SHA256SUMS.txt" for asset in assets):
        return None
    zips = [asset for asset in assets if asset.get("name", "").lower().endswith(".zip")]
    if len(zips) != 1:
        return None
    digest = zips[0].get("digest")
    return digest if isinstance(digest, str) and SHA256_DIGEST.fullmatch(digest) else None


def promoted_beta_matches(releases: list[dict]) -> list[tuple[dict, dict]]:
    stable_by_digest: dict[str, dict] = {}
    for release in sorted(releases, key=lambda item: item.get("tag_name", "")):
        if release.get("tag_name", "").startswith("surge-") and "-rls-v" in release["tag_name"]:
            if digest := uploaded_zip_digest(release):
                stable_by_digest[digest] = release
    matches = []
    for release in releases:
        tag = release.get("tag_name", "")
        if tag.startswith("surge-") and "-beta-v" in tag:
            if stable := stable_by_digest.get(uploaded_zip_digest(release)):
                matches.append((stable, release))
    return matches


def prune_promoted_betas(repo: str) -> int:
    removed = 0
    for stable, beta in promoted_beta_matches(list_releases(repo)):
        tag = beta["tag_name"]
        marker = f"<!-- promoted-beta: {tag} -->"
        body = stable.get("body") or ""
        if marker not in body:
            subprocess.run(
                ["gh", "api", "-X", "PATCH", f"repos/{repo}/releases/{stable['id']}",
                 "-f", f"body={body.rstrip()}\n\n{marker}\n"],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            stable["body"] = f"{body.rstrip()}\n\n{marker}\n"
        subprocess.run(["gh", "release", "delete", tag, "--repo", repo, "--cleanup-tag", "--yes"], check=True)
        print(f"Removed promoted beta {tag}; ZIP SHA-256 matches {stable['tag_name']}", flush=True)
        removed += 1
    return removed


def suppressed_beta_tags(releases: list[dict]) -> set[str]:
    return {
        match.group(1)
        for release in releases
        if "-rls-v" in release.get("tag_name", "")
        for match in PROMOTED_BETA.finditer(release.get("body") or "")
    }


def changelog_block(markdown: str) -> str:
    markdown = markdown.strip()
    if not markdown:
        return ""
    markdown = re.sub(r"\[([^\]]+)\]\((https?://(?:dl\.|www\.)?nssurge\.com/mac/[^)]+)\)",
                      r"\1", markdown, flags=re.IGNORECASE)
    markdown = SOURCE_ADDRESS.sub("[source address omitted]", markdown)
    return f"<!-- surge-changelog:start -->\n### Changelog\n\n{markdown}\n<!-- surge-changelog:end -->"


def with_changelog(body: str, markdown: str) -> str:
    block = changelog_block(markdown)
    if not block:
        return body
    if CHANGELOG_BLOCK.search(body):
        return CHANGELOG_BLOCK.sub(lambda _: block, body)
    return f"{body.rstrip()}\n\n{block}\n"


def sync_changelogs(repo: str, builds: list[Build]) -> int:
    builds_by_tag = {build.tag: build for build in builds}
    updated = 0
    for release in list_releases(repo):
        tag = release.get("tag_name", "")
        if not tag.startswith("surge-"):
            continue
        body = release.get("body") or ""
        new_body = RELEASE_BOILERPLATE.sub("", body)
        if build := builds_by_tag.get(tag):
            new_body = with_changelog(new_body, build.changelog)
        if new_body == body:
            continue
        subprocess.run(
            ["gh", "api", "-X", "PATCH", f"repos/{repo}/releases/{release['id']}",
             "-f", f"body={new_body}"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        print(f"Updated release notes {tag}", flush=True)
        updated += 1
    return updated


def release_is_incomplete(repo: str, build: Build) -> bool:
    result = subprocess.check_output(
        ["gh", "api", f"repos/{repo}/releases/tags/{urllib.parse.quote(build.tag, safe='')}",
         "--jq", '[.assets[] | select(.state == "uploaded") | .name]'],
        text=True,
    )
    return not {build.filename, "SHA256SUMS.txt"}.issubset(json.loads(result))


def planned_builds(channel: str, backfill: bool) -> tuple[list[Build], str]:
    stable = fetch_builds("rls", FEEDS["rls"])
    beta = fetch_builds("beta", FEEDS["beta"])
    latest_stable = max(stable, key=lambda build: build.number)
    stable_ids = {(build.version, build.number) for build in stable}
    beta_only = [build for build in beta if (build.version, build.number) not in stable_ids]

    if backfill and channel in ("rls", "all"):
        for feed_url in ARCHIVE_FEEDS:
            stable.extend(fetch_builds("rls", feed_url))
        stable.append(Build("rls", "2.6.7", 656, LEGACY_ZIP, LEGACY_CHANGELOG))

    candidates: list[Build] = []
    if channel in ("rls", "all"):
        candidates.extend(stable if backfill else [latest_stable])
    if channel in ("beta", "all"):
        candidates.extend(beta_only if backfill else sorted(beta_only, key=lambda build: build.number, reverse=True)[:1])
    unique = {build.tag: build for build in reversed(candidates)}
    ordered = sorted(unique.values(), key=lambda build: (build.channel != "rls", -build.number))
    priority = [build for build in ordered if build.tag == latest_stable.tag]
    priority.extend(build for build in ordered if build.channel == "beta")
    priority.extend(build for build in ordered if build.channel == "rls" and build.tag != latest_stable.tag)
    return priority, latest_stable.tag


def download(build: Build, destination: Path) -> str:
    validate_download_url(build.url)
    request = urllib.request.Request(build.url, headers={"User-Agent": "surge-release-mirror/1.0"})
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(request, timeout=90) as response, destination.open("wb") as output:
        validate_download_url(response.geturl())
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_ZIP_BYTES:
                raise ValueError(f"ZIP exceeds {MAX_ZIP_BYTES} bytes")
            output.write(chunk)
            digest.update(chunk)
    if not zipfile.is_zipfile(destination):
        raise ValueError(f"Invalid ZIP: {destination.name}")
    with zipfile.ZipFile(destination) as archive:
        bad_member = archive.testzip()
    if bad_member is not None:
        raise ValueError(f"Corrupt ZIP member: {bad_member}")
    return digest.hexdigest()


def publish(repo: str, build: Build, latest_stable_tag: str, repair: bool = False) -> None:
    with tempfile.TemporaryDirectory(prefix="surge-release-") as temporary:
        directory = Path(temporary)
        asset = directory / build.filename
        checksum = download(build, asset)
        checksums = directory / "SHA256SUMS.txt"
        checksums.write_text(f"{checksum}  {asset.name}\n", encoding="utf-8")
        notes = directory / "RELEASE_NOTES.md"
        channel_name = "Release" if build.channel == "rls" else "Beta"
        body = (
            f"## Surge for Mac {build.version} ({build.number}) · {channel_name}\n\n"
            f"- SHA-256: `{checksum}`\n"
        )
        notes.write_text(with_changelog(body, build.changelog), encoding="utf-8")
        if repair:
            command = ["gh", "release", "upload", build.tag, str(asset), str(checksums),
                       "--repo", repo, "--clobber"]
        else:
            command = [
                "gh", "release", "create", build.tag,
                "--repo", repo,
                "--title", f"Surge for Mac {build.version} ({build.number}) · {channel_name}",
                "--notes-file", str(notes),
                "--latest" if build.tag == latest_stable_tag else "--latest=false",
            ]
            command.extend((str(asset), str(checksums)))
        subprocess.run(command, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", choices=["rls", "beta", "all"], default="all")
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--max-per-run", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.max_per_run <= 128:
        parser.error("--max-per-run must be between 1 and 128")
    repo = os.environ.get("GITHUB_REPOSITORY", "LOVECHEN/surge-release")
    try:
        validate_sources(args.backfill)
        candidates, latest_stable_tag = planned_builds(args.channel, args.backfill)
        releases = [] if args.dry_run else list_releases(repo)
        mirrored = {release["tag_name"] for release in releases}
        suppressed = suppressed_beta_tags(releases)
    except Exception as exc:
        print(f"Discovery failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    pending = [
        build for build in candidates
        if build.tag not in suppressed and (build.tag not in mirrored or release_is_incomplete(repo, build))
    ]
    print(f"Discovered {len(candidates)} builds; {len(pending)} not mirrored", flush=True)
    if args.dry_run:
        for build in pending[:args.max_per_run]:
            print(build.tag)
        return 0
    failures = 0
    published = 0
    unavailable = 0
    for build in pending:
        if published >= args.max_per_run:
            break
        try:
            publish(repo, build, latest_stable_tag, repair=build.tag in mirrored)
            published += 1
            print(f"Published {build.tag} ({published}/{args.max_per_run})", flush=True)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and build.channel == "rls" and build.tag != latest_stable_tag:
                unavailable += 1
                print(f"Unavailable at source (HTTP 404): {build.tag}", flush=True)
                continue
            failures += 1
            print(f"Failed {build.tag}: HTTP {exc.code}", file=sys.stderr, flush=True)
        except Exception as exc:
            failures += 1
            print(f"Failed {build.tag}: {type(exc).__name__}", file=sys.stderr, flush=True)
    try:
        updated = sync_changelogs(repo, candidates)
    except Exception as exc:
        failures += 1
        print(f"Changelog sync failed: {type(exc).__name__}", file=sys.stderr, flush=True)
        updated = 0
    try:
        removed = prune_promoted_betas(repo)
    except Exception as exc:
        failures += 1
        print(f"Beta cleanup failed: {type(exc).__name__}", file=sys.stderr, flush=True)
        removed = 0
    print(f"Published {published}; updated changelogs {updated}; removed promoted betas {removed}; unavailable {unavailable}; remaining {len(pending) - published - unavailable}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
