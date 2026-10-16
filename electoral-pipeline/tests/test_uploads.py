import asyncio
from io import BytesIO

import pytest
from starlette.datastructures import Headers, UploadFile

from app import config
from app.uploads import UploadValidationError, safe_batch_name, safe_relative_pdf_path, save_upload_batch
from app.workflow import local_source_id


def upload(filename: str, content: bytes) -> UploadFile:
    return UploadFile(BytesIO(content), filename=filename, headers=Headers({"content-type": "application/pdf"}))


def test_safe_relative_path_preserves_nested_folders():
    assert str(safe_relative_pdf_path("state/district/roll.PDF")) == "state/district/roll.PDF"


@pytest.mark.parametrize("name", ["../roll.pdf", "/tmp/roll.pdf", "roll.txt", "folder/../../roll.pdf"])
def test_unsafe_or_non_pdf_paths_are_rejected(name):
    with pytest.raises(UploadValidationError):
        safe_relative_pdf_path(name)


def test_batch_name_is_safe():
    assert safe_batch_name(" Delhi / North ") == "Delhi-North"


def test_local_source_identity_preserves_folder_segregation(tmp_path):
    first = tmp_path / "north" / "roll.pdf"
    second = tmp_path / "south" / "roll.pdf"
    assert local_source_id(tmp_path, first, "same-hash") != local_source_id(tmp_path, second, "same-hash")
    assert local_source_id(tmp_path, first, "same-hash") == local_source_id(tmp_path, first, "same-hash")


def test_batch_upload_retains_structure_and_writes_complete_pdfs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DOCUMENT_ROOT", tmp_path)
    summary = asyncio.run(save_upload_batch([
        upload("north/one.pdf", b"%PDF-1.4\nfirst"),
        upload("south/deep/two.pdf", b"%PDF-1.4\nsecond"),
    ], "UAT batch"))

    batch = tmp_path / summary.relative_directory
    assert (batch / "north/one.pdf").read_bytes() == b"%PDF-1.4\nfirst"
    assert (batch / "south/deep/two.pdf").read_bytes() == b"%PDF-1.4\nsecond"
    assert list(batch.rglob("*.part")) == []
    assert summary.file_count == 2


def test_invalid_pdf_removes_the_whole_incomplete_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DOCUMENT_ROOT", tmp_path)
    with pytest.raises(UploadValidationError, match="not a valid PDF"):
        asyncio.run(save_upload_batch([upload("fake.pdf", b"not a pdf")], "bad"))
    assert list(tmp_path.iterdir()) == []
