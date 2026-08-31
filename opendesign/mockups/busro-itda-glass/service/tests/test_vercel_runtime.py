from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request

import busro_vercel_runtime as runtime
from busro_vercel_runtime import dispatch_request, reset_runtime_for_tests


ARCHIVE_URL = "https://catalog.example.test/releases/network_catalog.sqlite3.tar.gz"
GITHUB_RELEASE_URL = (
    "https://github.com/kuseumkkrkkr/busro-itda/releases/download/"
    "catalog-2026.08.31/network_catalog.sqlite3.tar.gz"
)
GITHUB_ASSET_URL = (
    "https://release-assets.githubusercontent.com/github-production-release-asset/"
    "123456/12345678-1234-1234-1234-123456789abc?sp=r&sig=secret%2Fvalue"
)


class _Headers:
    def __init__(self, content_length: int | None):
        self._content_length = content_length

    def get_all(self, name: str):
        if name.lower() != "content-length" or self._content_length is None:
            return None
        return [str(self._content_length)]

    def get(self, name: str, default=None):
        values = self.get_all(name)
        return default if values is None else values[0]


class _ArchiveResponse:
    def __init__(
        self,
        payload: bytes,
        *,
        content_length: int | None = None,
        final_url: str = ARCHIVE_URL,
    ):
        self._payload = io.BytesIO(payload)
        self.headers = _Headers(content_length)
        self.status = 200
        self._final_url = final_url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        return self._payload.read(size)

    def geturl(self) -> str:
        return self._final_url


def _sqlite_payload(size: int = 1_000_000) -> bytes:
    return b"SQLite format 3\x00" + (b"x" * (size - 16))


def _archive_payload(
    payload: bytes,
    *,
    member_name: str = runtime.CATALOG_ARCHIVE_MEMBER,
    member_type: bytes = tarfile.REGTYPE,
    extra_member: bool = False,
) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        member = tarfile.TarInfo(member_name)
        member.type = member_type
        if member_type in {tarfile.REGTYPE, tarfile.AREGTYPE}:
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        else:
            member.linkname = "network_catalog.sqlite3"
            archive.addfile(member)
        if extra_member:
            extra = tarfile.TarInfo("extra.txt")
            extra.size = 1
            archive.addfile(extra, io.BytesIO(b"x"))
    return output.getvalue()


class VercelRuntimeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.environment = patch.dict(
            os.environ,
            {
                "BUSRO_FIXTURE_MODE": "1",
                "BUSRO_DB_PATH": str(root / "observations.sqlite3"),
                "BUSRO_NETWORK_CATALOG_PATH": str(root / "catalog.sqlite3"),
            },
            clear=False,
        )
        self.environment.start()
        reset_runtime_for_tests()

    def tearDown(self) -> None:
        reset_runtime_for_tests()
        self.environment.stop()
        self.temporary.cleanup()

    def test_status_discloses_serverless_storage_boundary_without_secret(self) -> None:
        response = dispatch_request("GET", "/api/status")
        self.assertEqual(response.status, 200)
        self.assertFalse(response.payload["tago"]["key_exposed"])
        self.assertEqual(response.payload["deployment"]["platform"], "vercel")
        self.assertEqual(response.payload["deployment"]["observation_storage"], "ephemeral")
        self.assertFalse(response.payload["capabilities"]["snapshot_collection"])
        self.assertFalse(response.payload["capabilities"]["verified_route_hydration"])

    def test_persistent_mutations_fail_closed(self) -> None:
        for path in ("/api/collect", "/api/positions/collect", "/api/network/hydrate"):
            with self.subTest(path=path):
                response = dispatch_request("POST", path, body={})
                self.assertEqual(response.status, 503)
                self.assertEqual(response.payload["error"]["code"], "PERSISTENT_STORAGE_REQUIRED")

    def test_static_search_and_unknown_route_are_bounded(self) -> None:
        search = dispatch_request("GET", "/api/network/stops", {"q": "서울", "limit": "8"})
        self.assertEqual(search.status, 200)
        self.assertIn("s-maxage", search.cache_control)

        missing = dispatch_request("GET", "/api/not-real")
        self.assertEqual(missing.status, 404)
        self.assertEqual(missing.payload["error"]["code"], "NOT_FOUND")


class VercelCatalogArchiveCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.meta = self.root / "network_catalog.meta.json"
        self.environment = patch.dict(
            os.environ,
            {
                "BUSRO_RUNTIME_ROOT": str(self.root / "runtime"),
                "BUSRO_CATALOG_ARCHIVE_URL": ARCHIVE_URL,
                "BUSRO_CATALOG_ARCHIVE_ALLOWED_URLS": ARCHIVE_URL,
            },
            clear=False,
        )
        self.environment.start()
        self.module_paths = (
            patch.object(runtime, "CATALOG_META", self.meta),
            patch.object(runtime, "PACKAGED_CATALOG", self.root / "not-packaged.sqlite3"),
        )
        for module_path in self.module_paths:
            module_path.start()

    def tearDown(self) -> None:
        for module_path in reversed(self.module_paths):
            module_path.stop()
        self.environment.stop()
        self.temporary.cleanup()

    def _write_manifest(
        self,
        payload: bytes,
        archive: bytes,
        *,
        archive_sha256: str | None = None,
        archive_bytes: int | None = None,
    ) -> None:
        manifest = {
            "schema_version": 1,
            "uncompressed_bytes": len(payload),
            "uncompressed_sha256": hashlib.sha256(payload).hexdigest(),
        }
        if archive_sha256 is not None:
            manifest["archive_sha256"] = archive_sha256
        if archive_bytes is not None:
            manifest["archive_bytes"] = archive_bytes
        self.meta.write_text(json.dumps(manifest), encoding="utf-8")

    def test_remote_archive_is_validated_atomically_and_reused_without_redownload(self) -> None:
        payload = _sqlite_payload()
        archive = _archive_payload(payload)
        self._write_manifest(
            payload,
            archive,
            archive_sha256=hashlib.sha256(archive).hexdigest(),
            archive_bytes=len(archive),
        )
        response = _ArchiveResponse(archive, content_length=len(archive))

        with patch.object(runtime, "_open_archive_response", return_value=response) as opened:
            first = runtime._prepare_catalog_copy()
            second = runtime._prepare_catalog_copy()

        self.assertEqual(first, second)
        self.assertEqual(first.read_bytes(), payload)
        self.assertEqual(opened.call_count, 1)
        self.assertFalse(any(path.name.endswith(".tmp") for path in first.parent.iterdir()))

    def test_archive_digest_mismatch_fails_without_publishing_catalog(self) -> None:
        payload = _sqlite_payload()
        archive = _archive_payload(payload)
        self._write_manifest(payload, archive, archive_sha256="0" * 64)

        with patch.object(
            runtime,
            "_open_archive_response",
            return_value=_ArchiveResponse(archive, content_length=len(archive)),
        ):
            with self.assertRaisesRegex(RuntimeError, "digest validation failed"):
                runtime._prepare_catalog_copy()

        self.assertFalse(list((self.root / "runtime").glob("network-catalog-*.sqlite3")))

    def test_uncompressed_digest_and_sqlite_magic_are_both_required(self) -> None:
        payload = _sqlite_payload()
        archive = _archive_payload(payload)
        self._write_manifest(
            payload,
            archive,
            archive_sha256=hashlib.sha256(archive).hexdigest(),
        )
        manifest = json.loads(self.meta.read_text(encoding="utf-8"))
        manifest["uncompressed_sha256"] = "0" * 64
        self.meta.write_text(json.dumps(manifest), encoding="utf-8")
        with patch.object(
            runtime,
            "_open_archive_response",
            return_value=_ArchiveResponse(archive, content_length=len(archive)),
        ):
            with self.assertRaisesRegex(RuntimeError, "content validation failed"):
                runtime._prepare_catalog_copy()

        invalid_sqlite = b"not a sqlite db!" + (b"x" * (1_000_000 - 16))
        invalid_archive = _archive_payload(invalid_sqlite)
        self._write_manifest(
            invalid_sqlite,
            invalid_archive,
            archive_sha256=hashlib.sha256(invalid_archive).hexdigest(),
        )
        with patch.object(
            runtime,
            "_open_archive_response",
            return_value=_ArchiveResponse(
                invalid_archive,
                content_length=len(invalid_archive),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "SQLite validation failed"):
                runtime._prepare_catalog_copy()

    def test_packaged_catalog_remains_the_no_url_fallback(self) -> None:
        payload = _sqlite_payload()
        archive = _archive_payload(payload)
        self._write_manifest(payload, archive)
        runtime.PACKAGED_CATALOG.write_bytes(payload)

        with patch.dict(os.environ, {"BUSRO_CATALOG_ARCHIVE_URL": ""}, clear=False), patch.object(
            runtime,
            "_open_archive_response",
        ) as opened:
            target = runtime._prepare_catalog_copy()

        self.assertEqual(target.read_bytes(), payload)
        opened.assert_not_called()

    def test_changed_final_url_is_rejected_without_disclosing_it(self) -> None:
        payload = _sqlite_payload()
        archive = _archive_payload(payload)
        self._write_manifest(payload, archive)
        redirected = "https://evil.example/secret-token/catalog.tar.gz"

        with patch.object(
            runtime,
            "_open_archive_response",
            return_value=_ArchiveResponse(
                archive,
                content_length=len(archive),
                final_url=redirected,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "redirect is not allowed") as raised:
                runtime._prepare_catalog_copy()
        self.assertNotIn(redirected, str(raised.exception))

    def test_github_release_asset_redirect_is_revalidated_and_downloaded(self) -> None:
        payload = _sqlite_payload()
        archive = _archive_payload(payload)
        self._write_manifest(
            payload,
            archive,
            archive_sha256=hashlib.sha256(archive).hexdigest(),
        )

        with patch.dict(
            os.environ,
            {"BUSRO_CATALOG_ARCHIVE_URL": GITHUB_RELEASE_URL},
            clear=False,
        ), patch.object(
            runtime,
            "_open_archive_response",
            return_value=_ArchiveResponse(
                archive,
                content_length=len(archive),
                final_url=GITHUB_ASSET_URL,
            ),
        ):
            target = runtime._prepare_catalog_copy()

        self.assertEqual(target.read_bytes(), payload)

    def test_redirect_handler_allows_one_github_asset_hop_only(self) -> None:
        handler = runtime._CatalogRedirectHandler(GITHUB_RELEASE_URL)
        initial_request = Request(GITHUB_RELEASE_URL)
        redirected_request = handler.redirect_request(
            initial_request,
            None,
            302,
            "Found",
            {},
            GITHUB_ASSET_URL,
        )
        self.assertIsNotNone(redirected_request)
        self.assertEqual(redirected_request.full_url, GITHUB_ASSET_URL)
        self.assertIsNone(
            handler.redirect_request(
                redirected_request,
                None,
                302,
                "Found",
                {},
                GITHUB_ASSET_URL,
            )
        )

        supabase_url = (
            "https://abcdefghijklmnopqrst.supabase.co/storage/v1/object/public/"
            "catalog/network_catalog-a1.tar.gz"
        )
        self.assertIsNone(
            runtime._CatalogRedirectHandler(supabase_url).redirect_request(
                Request(supabase_url),
                None,
                302,
                "Found",
                {},
                GITHUB_ASSET_URL,
            )
        )

    def test_content_length_and_stream_are_both_bounded(self) -> None:
        payload = _sqlite_payload()
        archive = _archive_payload(payload)
        self._write_manifest(payload, archive)
        cases = (
            _ArchiveResponse(b"", content_length=65),
            _ArchiveResponse(b"x" * 65, content_length=None),
        )
        for response in cases:
            with self.subTest(content_length=response.headers.get("Content-Length")):
                with patch.object(runtime, "MAX_ARCHIVE_BYTES", 64), patch.object(
                    runtime,
                    "_open_archive_response",
                    return_value=response,
                ):
                    with self.assertRaisesRegex(RuntimeError, "compressed size limit"):
                        runtime._prepare_catalog_copy()

    def test_traversal_symlink_and_multiple_members_are_rejected(self) -> None:
        payload = _sqlite_payload()
        cases = (
            _archive_payload(payload, member_name="../network_catalog.sqlite3"),
            _archive_payload(payload, member_type=tarfile.SYMTYPE),
            _archive_payload(payload, extra_member=True),
        )
        for archive in cases:
            with self.subTest(archive_sha256=hashlib.sha256(archive).hexdigest()):
                self._write_manifest(
                    payload,
                    archive,
                    archive_sha256=hashlib.sha256(archive).hexdigest(),
                )
                with patch.object(
                    runtime,
                    "_open_archive_response",
                    return_value=_ArchiveResponse(archive, content_length=len(archive)),
                ):
                    with self.assertRaisesRegex(RuntimeError, "structure|exactly one"):
                        runtime._prepare_catalog_copy()
        self.assertFalse(list((self.root / "runtime").glob("network-catalog-*.sqlite3")))

    def test_url_policy_accepts_public_supabase_or_exact_allowlist_only(self) -> None:
        supabase_url = (
            "https://abcdefghijklmnopqrst.supabase.co/storage/v1/object/public/"
            "catalog/network_catalog-a1.tar.gz"
        )
        self.assertEqual(runtime._validated_archive_url(supabase_url), supabase_url)
        self.assertEqual(runtime._validated_archive_url(ARCHIVE_URL), ARCHIVE_URL)
        self.assertEqual(runtime._validated_archive_url(GITHUB_RELEASE_URL), GITHUB_RELEASE_URL)

        denied = (
            "http://abcdefghijklmnopqrst.supabase.co/storage/v1/object/public/catalog/a.tar.gz",
            "https://user@abcdefghijklmnopqrst.supabase.co/storage/v1/object/public/catalog/a.tar.gz",
            "https://abcdefghijklmnopqrst.supabase.co/storage/v1/object/public/catalog/a.tar.gz?token=secret",
            "https://abcdefghijklmnopqrst.supabase.co/storage/v1/object/public/catalog/a.tar.gz#fragment",
            "https://abcdefghijklmnopqrst.supabase.co/storage/v1/object/private/catalog/a.tar.gz",
            "https://evil.example/storage/v1/object/public/catalog/a.tar.gz",
            "https://github.com/kuseumkkrkkr/busro-itda/releases/latest/download/network_catalog.sqlite3.tar.gz",
            "https://github.com/kuseumkkrkkr/busro-itda/releases/download/latest/network_catalog.sqlite3.tar.gz",
        )
        for value in denied:
            with self.subTest(value=value), self.assertRaises(RuntimeError) as raised:
                runtime._validated_archive_url(value)
            self.assertNotIn(value, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
