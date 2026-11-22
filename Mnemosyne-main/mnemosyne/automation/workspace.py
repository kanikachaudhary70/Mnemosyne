"""
Mnemosyne Automation - Isolated Workspace Manager
Provides secure, sandboxed working directories for analyzing and patching untrusted code.
Guards against path traversal, restricts file types, preserves originals, and enables rollback.
"""
import os
import shutil
import difflib
import tempfile
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from mnemosyne.automation.models import PatchDiff

ALLOWED_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx",
    ".json", ".html", ".css", ".yaml", ".yml",
    ".md", ".txt", ".sql"
}
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB


class WorkspaceSecurityError(Exception):
    """Raised when an untrusted file or path fails security boundaries."""
    pass


class WorkspaceManager:
    """Manages an isolated workspace for a single automation job."""

    def __init__(self, job_id: str, base_dir: Optional[Path] = None):
        self.job_id = job_id
        if base_dir:
            self.root = base_dir / ".mnemosyne" / "workspaces" / job_id
        else:
            self.root = Path(tempfile.gettempdir()) / "mnemosyne_workspaces" / job_id
        self.root.mkdir(parents=True, exist_ok=True)
        # Store backup copy of original files for clean rollback
        self._originals: Dict[str, str] = {}

    def sanitize_rel_path(self, rel_path: str) -> Path:
        """Validate and resolve relative path, preventing directory traversal."""
        clean = Path(rel_path).as_posix().lstrip("/\\")
        # Check for traversal attempts
        parts = Path(clean).parts
        if ".." in parts or any(p.startswith("..") for p in parts):
            raise WorkspaceSecurityError(f"Path traversal detected: {rel_path}")

        target = (self.root / clean).resolve()
        # Verify it stays strictly within workspace root
        if not str(target).startswith(str(self.root.resolve())):
            raise WorkspaceSecurityError(f"Target path escapes workspace boundaries: {rel_path}")
        return target

    def validate_file(self, filename: str, content: bytes) -> None:
        """Enforce extension and size constraints on incoming files."""
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise WorkspaceSecurityError(f"Unsupported or disallowed file extension: {ext}")
        if len(content) > MAX_FILE_SIZE_BYTES:
            raise WorkspaceSecurityError(f"File size exceeds limit ({len(content)} > {MAX_FILE_SIZE_BYTES} bytes)")

    def import_file(self, rel_path: str, content_bytes: bytes) -> Path:
        """Import an uploaded file safely into the workspace."""
        self.validate_file(rel_path, content_bytes)
        target = self.sanitize_rel_path(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content_bytes)
        try:
            text = content_bytes.decode("utf-8", errors="replace")
            self._originals[rel_path] = text
        except Exception:
            pass
        return target

    def copy_from_repo(self, repo_file: Path, rel_path: Optional[str] = None) -> Path:
        """Copy an existing file from the local repository into this workspace."""
        if not repo_file.exists():
            raise FileNotFoundError(f"Source file not found: {repo_file}")
        rel = rel_path or repo_file.name
        content = repo_file.read_bytes()
        return self.import_file(rel, content)

    def read_file(self, rel_path: str) -> str:
        """Read text content of a file in workspace."""
        target = self.sanitize_rel_path(rel_path)
        if not target.exists():
            raise FileNotFoundError(f"File does not exist in workspace: {rel_path}")
        return target.read_text(encoding="utf-8", errors="replace")

    def write_file(self, rel_path: str, text: str) -> Path:
        """Write modified content to a file in workspace."""
        target = self.sanitize_rel_path(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Record original if not recorded
        if rel_path not in self._originals and target.exists():
            self._originals[rel_path] = target.read_text(encoding="utf-8", errors="replace")
        target.write_text(text, encoding="utf-8")
        return target

    def compute_diff(self, rel_path: str) -> PatchDiff:
        """Compute unified diff between original and current file content."""
        target = self.sanitize_rel_path(rel_path)
        current = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
        original = self._originals.get(rel_path, current)

        orig_lines = original.splitlines(keepends=True)
        curr_lines = current.splitlines(keepends=True)

        diff_lines = list(difflib.unified_diff(
            orig_lines,
            curr_lines,
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
        ))
        diff_unified = "".join(diff_lines)

        return PatchDiff(
            file_path=rel_path,
            original_content=original,
            patched_content=current,
            diff_unified=diff_unified,
            is_safe=True,
        )

    def rollback(self, rel_path: Optional[str] = None) -> None:
        """Roll back one file or all files to their original state."""
        if rel_path:
            if rel_path in self._originals:
                self.write_file(rel_path, self._originals[rel_path])
        else:
            for path, orig in self._originals.items():
                target = self.sanitize_rel_path(path)
                target.write_text(orig, encoding="utf-8")

    def list_files(self) -> List[str]:
        """List all tracked relative files in this workspace."""
        res = []
        for p in self.root.rglob("*"):
            if p.is_file():
                rel = p.relative_to(self.root).as_posix()
                res.append(rel)
        return res

    def cleanup(self) -> None:
        """Remove temporary workspace directory."""
        if self.root.exists():
            try:
                shutil.rmtree(self.root)
            except Exception:
                pass
