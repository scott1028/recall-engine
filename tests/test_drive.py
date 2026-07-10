import io
from types import SimpleNamespace
from unittest.mock import MagicMock

import httplib2
import pytest
from googleapiclient.errors import HttpError
from typer.testing import CliRunner

from recall_engine import drive
from recall_engine.cli import app
from recall_engine.drive import (
    AUTH_HELP,
    FOLDER_MIME,
    DriveError,
    build_drive_service,
    resolve_folder_id,
    sync_download,
    sync_upload,
)

runner = CliRunner()

GCLOUD_COMMAND = (
    "gcloud auth application-default login "
    "--scopes=https://www.googleapis.com/auth/drive,"
    "https://www.googleapis.com/auth/cloud-platform"
)
GCLOUD_LOGIN_COMMAND = "gcloud auth login --enable-gdrive-access"


def make_service(list_pages: list[dict]) -> MagicMock:
    """Build a mock Drive service whose files().list().execute() pages through list_pages."""
    service = MagicMock()
    service.files.return_value.list.return_value.execute.side_effect = list_pages
    return service


def install_fake_downloader(monkeypatch, content_by_id: dict[str, bytes]) -> None:
    """Replace MediaIoBaseDownload with a fake that writes canned bytes per fileId."""
    class FakeDownloader:
        def __init__(self, fh: io.BytesIO, request) -> None:
            self._fh = fh
            self._request = request

        def next_chunk(self):
            self._fh.write(content_by_id[self._request.file_id])
            return None, True

    monkeypatch.setattr(drive, "MediaIoBaseDownload", FakeDownloader)


def make_http_error(status: int, content: bytes) -> HttpError:
    return HttpError(resp=httplib2.Response({"status": str(status)}), content=content)


def test_download_writes_plain_md_and_skips_non_md(monkeypatch, tmp_path):
    service = make_service(
        [
            {
                "files": [
                    {
                        "id": "f1",
                        "name": "note.md",
                        "mimeType": "text/markdown",
                        "modifiedTime": "2026-01-01T00:00:00Z",
                    },
                    {
                        "id": "f2",
                        "name": "photo.png",
                        "mimeType": "image/png",
                        "modifiedTime": "2026-01-01T00:00:00Z",
                    },
                ]
            }
        ]
    )
    service.files.return_value.get_media.side_effect = (
        lambda fileId: SimpleNamespace(file_id=fileId)
    )
    install_fake_downloader(monkeypatch, {"f1": b"# note\n"})
    dest = tmp_path / "src"

    written = sync_download(service, "folder1", dest)

    assert written == ["note.md"]
    assert (dest / "note.md").read_bytes() == b"# note\n"
    assert not (dest / "photo.png").exists()
    service.files.return_value.list.assert_called_once_with(
        q="'folder1' in parents and trashed = false",
        fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
        pageToken=None,
    )


def test_download_exports_native_doc_as_md(tmp_path):
    service = make_service(
        [
            {
                "files": [
                    {
                        "id": "d1",
                        "name": "design notes",
                        "mimeType": "application/vnd.google-apps.document",
                        "modifiedTime": "2026-01-01T00:00:00Z",
                    }
                ]
            }
        ]
    )
    # The plain-text export carries a leading BOM and CRLF line endings.
    service.files.return_value.export.return_value.execute.return_value = (
        b"\xef\xbb\xbf# doc\r\nbody\r\n"
    )
    dest = tmp_path / "src"

    written = sync_download(service, "folder1", dest)

    assert written == ["design notes.md"]
    assert (dest / "design notes.md").read_bytes() == b"# doc\nbody\n"
    service.files.return_value.export.assert_called_once_with(
        fileId="d1", mimeType="text/plain"
    )


def test_download_duplicate_name_keeps_latest_and_warns(monkeypatch, tmp_path, capsys):
    service = make_service(
        [
            {
                "files": [
                    {
                        "id": "old",
                        "name": "dup.md",
                        "mimeType": "text/markdown",
                        "modifiedTime": "2026-01-01T00:00:00Z",
                    },
                    {
                        "id": "new",
                        "name": "dup.md",
                        "mimeType": "text/markdown",
                        "modifiedTime": "2026-02-01T00:00:00Z",
                    },
                ]
            }
        ]
    )
    service.files.return_value.get_media.side_effect = (
        lambda fileId: SimpleNamespace(file_id=fileId)
    )
    install_fake_downloader(monkeypatch, {"old": b"old\n", "new": b"new\n"})
    dest = tmp_path / "src"

    written = sync_download(service, "folder1", dest)

    assert written == ["dup.md"]
    assert (dest / "dup.md").read_bytes() == b"new\n"
    assert "dup.md" in capsys.readouterr().err


def test_download_paginates_and_overwrites_existing_file(monkeypatch, tmp_path):
    service = make_service(
        [
            {
                "files": [
                    {
                        "id": "f1",
                        "name": "a.md",
                        "mimeType": "text/markdown",
                        "modifiedTime": "2026-01-01T00:00:00Z",
                    }
                ],
                "nextPageToken": "tok",
            },
            {
                "files": [
                    {
                        "id": "f2",
                        "name": "b.md",
                        "mimeType": "text/markdown",
                        "modifiedTime": "2026-01-01T00:00:00Z",
                    }
                ]
            },
        ]
    )
    service.files.return_value.get_media.side_effect = (
        lambda fileId: SimpleNamespace(file_id=fileId)
    )
    install_fake_downloader(monkeypatch, {"f1": b"A\n", "f2": b"B\n"})
    dest = tmp_path / "src"
    dest.mkdir()
    (dest / "a.md").write_text("stale local content\n")

    written = sync_download(service, "folder1", dest)

    assert sorted(written) == ["a.md", "b.md"]
    assert (dest / "a.md").read_bytes() == b"A\n"
    assert (dest / "b.md").read_bytes() == b"B\n"
    list_mock = service.files.return_value.list
    assert list_mock.call_count == 2
    assert list_mock.call_args_list[1].kwargs["pageToken"] == "tok"


def test_download_403_insufficient_scope_raises_drive_error(tmp_path):
    service = MagicMock()
    service.files.return_value.list.return_value.execute.side_effect = make_http_error(
        403,
        b'{"error": {"message": "Request had insufficient authentication scopes."}}',
    )

    with pytest.raises(DriveError) as excinfo:
        sync_download(service, "folder1", tmp_path / "src")
    assert GCLOUD_COMMAND in str(excinfo.value)


def make_folder_lookup_service(list_pages: list[dict]) -> MagicMock:
    """Mock service whose files().get 404s so resolve_folder_id searches by name."""
    service = make_service(list_pages)
    service.files.return_value.get.return_value.execute.side_effect = make_http_error(
        404, b'{"error": {"message": "File not found: ."}}'
    )
    return service


def test_resolve_folder_id_accepts_folder_id():
    service = MagicMock()
    service.files.return_value.get.return_value.execute.return_value = {
        "id": "folder1",
        "mimeType": FOLDER_MIME,
    }

    assert resolve_folder_id(service, "folder1") == "folder1"
    service.files.return_value.list.assert_not_called()


def test_resolve_folder_id_matches_name_case_insensitively():
    service = make_folder_lookup_service(
        [{"files": [{"id": "id1", "name": "Shared"}, {"id": "id2", "name": "shared-notes"}]}]
    )

    assert resolve_folder_id(service, "shared") == "id1"
    query = service.files.return_value.list.call_args.kwargs["q"]
    assert f"mimeType = '{FOLDER_MIME}'" in query
    assert "name contains 'shared'" in query
    assert "trashed = false" in query


def test_resolve_folder_id_escapes_single_quote_in_name():
    service = make_folder_lookup_service([{"files": [{"id": "id1", "name": "it's"}]}])

    assert resolve_folder_id(service, "it's") == "id1"
    query = service.files.return_value.list.call_args.kwargs["q"]
    assert "name contains 'it\\'s'" in query


def test_resolve_folder_id_not_found_raises():
    service = make_folder_lookup_service([{"files": []}])

    with pytest.raises(DriveError, match="Drive folder not found: 'missing'"):
        resolve_folder_id(service, "missing")


def test_resolve_folder_id_ambiguous_name_raises_with_candidates():
    service = make_folder_lookup_service(
        [{"files": [{"id": "id1", "name": "Shared"}, {"id": "id2", "name": "shared"}]}]
    )

    with pytest.raises(DriveError) as excinfo:
        resolve_folder_id(service, "shared")
    message = str(excinfo.value)
    assert "Multiple Drive folders named 'shared'" in message
    assert "Shared (id1)" in message
    assert "shared (id2)" in message
    assert "folder ID" in message


def test_resolve_folder_id_non_404_get_error_propagates():
    service = MagicMock()
    service.files.return_value.get.return_value.execute.side_effect = make_http_error(
        403,
        b'{"error": {"message": "Request had insufficient authentication scopes."}}',
    )

    with pytest.raises(DriveError, match="403"):
        resolve_folder_id(service, "shared")
    service.files.return_value.list.assert_not_called()


def test_upload_creates_new_and_updates_existing(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "existing.md").write_text("existing\n")
    (src / "fresh.md").write_text("fresh\n")
    # 'existing.md' matches a Drive file; 'fresh.md' does not.
    service = make_service([{"files": [{"id": "e1", "name": "existing.md"}]}, {"files": []}])

    uploaded = sync_upload(service, "folder1", src)

    assert uploaded == ["existing.md", "fresh.md"]
    files_mock = service.files.return_value
    files_mock.update.assert_called_once()
    assert files_mock.update.call_args.kwargs["fileId"] == "e1"
    files_mock.create.assert_called_once()
    create_kwargs = files_mock.create.call_args.kwargs
    assert create_kwargs["body"] == {"name": "fresh.md", "parents": ["folder1"]}
    # Plain markdown upload; never converted to a native Google Doc.
    assert create_kwargs["media_body"].mimetype() == "text/markdown"


def test_upload_escapes_single_quote_in_name_query(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "it's.md").write_text("quoted\n")
    service = make_service([{"files": []}])

    uploaded = sync_upload(service, "folder1", src)

    assert uploaded == ["it's.md"]
    query = service.files.return_value.list.call_args.kwargs["q"]
    assert "name = 'it\\'s.md'" in query


def test_upload_missing_src_dir_raises_drive_error(tmp_path):
    with pytest.raises(DriveError, match="Nothing to upload"):
        sync_upload(MagicMock(), "folder1", tmp_path / "missing")


def test_upload_empty_src_dir_raises_drive_error(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    with pytest.raises(DriveError, match="Nothing to upload"):
        sync_upload(MagicMock(), "folder1", src)


def install_missing_adc(monkeypatch) -> None:
    """Make google.auth.default fail as if no ADC file exists."""
    from google.auth.exceptions import DefaultCredentialsError

    def raise_no_creds(scopes):
        raise DefaultCredentialsError("no ADC")

    monkeypatch.setattr(drive.google.auth, "default", raise_no_creds)


def test_build_drive_service_without_any_credentials_shows_both_logins(monkeypatch):
    install_missing_adc(monkeypatch)
    monkeypatch.setattr(drive.shutil, "which", lambda name: None)

    with pytest.raises(DriveError) as excinfo:
        build_drive_service()
    assert GCLOUD_COMMAND in str(excinfo.value)
    assert GCLOUD_LOGIN_COMMAND in str(excinfo.value)
    assert str(excinfo.value) == AUTH_HELP


def test_build_drive_service_falls_back_to_gcloud_user_token(monkeypatch):
    install_missing_adc(monkeypatch)
    monkeypatch.setattr(drive.shutil, "which", lambda name: "/usr/bin/gcloud")
    monkeypatch.setattr(
        drive.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="tok123\n"),
    )
    built = {}

    def fake_build(name, version, credentials, cache_discovery):
        built["credentials"] = credentials
        return "service"

    monkeypatch.setattr(drive, "build", fake_build)

    assert build_drive_service() == "service"
    assert built["credentials"].token == "tok123"


def test_build_drive_service_gcloud_token_failure_raises_drive_error(monkeypatch):
    install_missing_adc(monkeypatch)
    monkeypatch.setattr(drive.shutil, "which", lambda name: "/usr/bin/gcloud")
    monkeypatch.setattr(
        drive.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""),
    )

    with pytest.raises(DriveError) as excinfo:
        build_drive_service()
    assert str(excinfo.value) == AUTH_HELP


def test_cli_sync_without_drive_folder_exits_2(monkeypatch, tmp_path):
    monkeypatch.setenv("KNOWLEDGE_REPO_PATH", str(tmp_path))
    monkeypatch.delenv("KNOWLEDGE_REPO_SSH", raising=False)
    monkeypatch.delenv("KNOWLEDGE_DRIVE_FOLDER", raising=False)

    result = runner.invoke(app, ["sync", "download"])

    assert result.exit_code == 2
    assert "KNOWLEDGE_DRIVE_FOLDER" in result.output


def test_cli_sync_download_happy_path(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("KNOWLEDGE_REPO_PATH", str(repo))
    monkeypatch.delenv("KNOWLEDGE_REPO_SSH", raising=False)
    monkeypatch.setenv("KNOWLEDGE_DRIVE_FOLDER", "folder123")
    calls = {}
    monkeypatch.setattr("recall_engine.cli.build_drive_service", lambda: "service")
    monkeypatch.setattr(
        "recall_engine.cli.resolve_folder_id", lambda service, folder: f"id:{folder}"
    )

    def fake_sync_download(service, folder_id, dest):
        calls["args"] = (service, folder_id, dest)
        return ["a.md", "b.md"]

    monkeypatch.setattr("recall_engine.cli.sync_download", fake_sync_download)

    result = runner.invoke(app, ["sync", "download"])

    assert result.exit_code == 0
    assert calls["args"] == ("service", "id:folder123", repo.resolve() / "src")
    assert "a.md" in result.output
    assert "synced 2 file(s) (download)" in result.output
