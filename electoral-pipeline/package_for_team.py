"""Create a shareable source ZIP without local secrets or runtime state."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


root = Path(__file__).resolve().parent
archive = root.parent / "electoral-pipeline-team.zip"
excluded_parts = {".git", "__pycache__", ".pytest_cache", ".venv"}
excluded_names = {".env", ".DS_Store"}

with ZipFile(archive, "w", ZIP_DEFLATED) as output:
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in excluded_names or any(part in excluded_parts for part in path.relative_to(root).parts):
            continue
        output.write(path, Path(root.name) / path.relative_to(root))
print(archive)
