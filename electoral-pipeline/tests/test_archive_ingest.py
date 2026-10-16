import zipfile

import pytest

from app.archive_ingest import extract_pdf_archive
from app.uploads import UploadValidationError


def make_zip(path, entries):
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


def test_zip_extraction_preserves_nested_pdf_folders(tmp_path):
    archive = tmp_path / "batch.zip"
    output = tmp_path / ".staging"
    make_zip(archive, {
        "state/north/one.pdf": b"%PDF-1.4\none",
        "state/south/two.PDF": b"%PDF-1.4\ntwo",
        "notes.txt": b"ignored",
    })

    count, size = extract_pdf_archive(archive, output)

    assert count == 2
    assert size == len(b"%PDF-1.4\none") + len(b"%PDF-1.4\ntwo")
    assert (output / "state/north/one.pdf").is_file()
    assert (output / "state/south/two.PDF").is_file()
    assert not (output / "notes.txt").exists()


def test_a_large_archive_is_not_refused_for_its_size(tmp_path):
    """A multi-GB booth archive is what actually arrives here, so it must land.

    The size ceiling was removed outright: it produced a Failed row whose
    error message was the limit, with the PDFs never reaching /uploads. This
    pins the behaviour so a well-meaning cap does not come back -- the bytes
    are written rather than counted against a ceiling.
    """
    archive = tmp_path / "big.zip"
    output = tmp_path / ".staging"
    filler = b"0" * 1024
    make_zip(archive, {f"rolls/{i:04d}.pdf": b"%PDF-1.4\n" + filler for i in range(200)})

    count, size = extract_pdf_archive(archive, output)

    assert count == 200
    assert size > 0
    assert (output / "rolls/0199.pdf").is_file()


def test_zip_path_traversal_is_rejected_and_cleaned(tmp_path):
    archive = tmp_path / "unsafe.zip"
    output = tmp_path / ".staging"
    make_zip(archive, {"../escape.pdf": b"%PDF-1.4\n"})

    with pytest.raises(UploadValidationError, match="Unsafe upload path"):
        extract_pdf_archive(archive, output)

    assert not output.exists()
    assert not (tmp_path / "escape.pdf").exists()


def test_zip_requires_at_least_one_pdf(tmp_path):
    archive = tmp_path / "empty.zip"
    output = tmp_path / ".staging"
    make_zip(archive, {"readme.txt": b"nothing to process"})

    with pytest.raises(UploadValidationError, match="does not contain"):
        extract_pdf_archive(archive, output)
