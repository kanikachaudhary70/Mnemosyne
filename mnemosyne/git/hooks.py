import os
import stat
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
from mnemosyne.git.inspector import GitInspector
from mnemosyne.memory.client import MemoryClient
from mnemosyne.config import load_config, save_config, find_project_root

logger = logging.getLogger(__name__)

PRE_COMMIT_SCRIPT = """#!/usr/bin/env bash
# Mnemosyne Git Pre-Commit Hook
# Validates staged changes against security vulnerabilities, syntax errors, and historical bugs.
# Returns non-zero exit code on critical findings to block unsafe commits.

if command -v mnemosyne > /dev/null 2>&1; then
    mnemosyne hook run --stage pre-commit
    exit $?
else
    # Fallback to python execution without unsafe error suppression
    python -m mnemosyne.cli hook run --stage pre-commit
    exit $?
fi
"""

POST_COMMIT_SCRIPT = """#!/usr/bin/env bash
# Mnemosyne Git Post-Commit Hook
# Auto-ingests commit metadata into Cognee knowledge graph after each commit.

if command -v mnemosyne > /dev/null 2>&1; then
    mnemosyne hook run --stage post-commit --silent 2>/dev/null || true
else
    python -m mnemosyne.cli hook run --stage post-commit --silent 2>/dev/null || true
fi
"""


class HookManager:
    """Manages git hooks installation and pre/post-commit memory execution."""

    @staticmethod
    def get_hooks_dir(repo_root: Optional[Path] = None) -> Optional[Path]:
        root = repo_root or find_project_root()
        git_dir = root / ".git"
        if not git_dir.exists():
            return None
        hooks_dir = git_dir / "hooks"
        hooks_dir.mkdir(exist_ok=True)
        return hooks_dir

    @classmethod
    def install_pre_commit_hook(cls, repo_root: Optional[Path] = None) -> bool:
        """Install the pre-commit hook that queries Cognee before each commit."""
        hooks_dir = cls.get_hooks_dir(repo_root)
        if not hooks_dir:
            return False

        hook_path = hooks_dir / "pre-commit"
        with open(hook_path, "w", encoding="utf-8") as f:
            f.write(PRE_COMMIT_SCRIPT)

        st = os.stat(hook_path)
        os.chmod(hook_path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        config = load_config(repo_root)
        config["hooks_installed"] = True
        save_config(config, repo_root)
        return True

    @classmethod
    def install_post_commit_hook(cls, repo_root: Optional[Path] = None) -> bool:
        """
        Install the post-commit hook that auto-ingests commits into Cognee.

        This closes the feedback loop: every commit is automatically stored
        in codebase memory without requiring manual 'mnemosyne remember'.
        """
        hooks_dir = cls.get_hooks_dir(repo_root)
        if not hooks_dir:
            return False

        hook_path = hooks_dir / "post-commit"
        with open(hook_path, "w", encoding="utf-8") as f:
            f.write(POST_COMMIT_SCRIPT)

        st = os.stat(hook_path)
        os.chmod(hook_path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        config = load_config(repo_root)
        config["post_commit_hook_installed"] = True
        save_config(config, repo_root)
        return True

    @classmethod
    def install_all_hooks(cls, repo_root: Optional[Path] = None) -> Dict[str, bool]:
        """Install both pre-commit and post-commit hooks."""
        return {
            "pre_commit": cls.install_pre_commit_hook(repo_root),
            "post_commit": cls.install_post_commit_hook(repo_root),
        }

    @classmethod
    def uninstall_hooks(cls, repo_root: Optional[Path] = None) -> bool:
        hooks_dir = cls.get_hooks_dir(repo_root)
        if not hooks_dir:
            return False

        for hook_name in ("pre-commit", "post-commit"):
            hook_path = hooks_dir / hook_name
            if hook_path.exists():
                try:
                    hook_path.unlink()
                except Exception:
                    pass

        config = load_config(repo_root)
        config["hooks_installed"] = False
        config["post_commit_hook_installed"] = False
        save_config(config, repo_root)
        return True

    @classmethod
    def execute_pre_commit_check(cls, repo_root: Optional[Path] = None) -> Dict[str, Any]:
        """
        [COGNEE & AUTOMATION GATE: pre-commit]
        Validates exact staged content and staged diff:
        1. Scans staged changes with MultiLayerScanner for syntax and security defects.
        2. Recalls related past bugs and rules from Cognee memory.
        3. Flags blocking findings (Critical and High) to reject unsafe commits.
        """
        from mnemosyne.automation.scanner import MultiLayerScanner
        from mnemosyne.automation.models import Severity

        root = repo_root or find_project_root()
        staged_files = GitInspector.get_staged_files(root)
        staged_diff = GitInspector.get_staged_diff(root)

        client = MemoryClient(root)
        scanner = MultiLayerScanner(memory_client=client, repo_root=root)
        warnings = []
        all_findings = []

        if staged_files or staged_diff:
            # 1. Scan staged diff
            if staged_diff:
                diff_findings = scanner.scan_code(staged_diff, "staged diff", is_diff=True)
                all_findings.extend(diff_findings)

            # 2. Scope recall to today's coding session
            session_id = f"precommit_{datetime.now().strftime('%Y%m%d')}"
            query = f"staged files: {' '.join(staged_files[:5])} {staged_diff[:300]}"

            if client._init_cognee_if_needed():
                try:
                    import cognee
                    session_context = (
                        f"Pre-commit: engineer staging changes to "
                        f"{', '.join(staged_files[:5])}. "
                        f"Diff preview: {staged_diff[:300]}"
                    )
                    asyncio.run(cognee.remember(session_context, session_id=session_id))
                except Exception as e:
                    logger.debug(f"Session memory update failed: {e}")

            # 3. Analyze exact staged content of each staged file
            for file_path in staged_files:
                staged_text = GitInspector.get_staged_content(root, file_path)
                if staged_text:
                    file_findings = scanner.scan_code(staged_text, file_path)
                    all_findings.extend(file_findings)

                recalled = client.recall(
                    query=query,
                    file_context=file_path,
                    top_k=3,
                    session_id=session_id,
                )
                for mem in recalled:
                    if mem not in warnings:
                        warnings.append(mem)

        # Blocking findings: any Critical or High severity defect
        blocking_findings = [
            f for f in all_findings
            if f.severity in (Severity.CRITICAL, Severity.HIGH)
        ]

        return {
            "staged_files": staged_files,
            "warnings": warnings,
            "findings": all_findings,
            "blocking_findings": blocking_findings,
            "blocked": len(blocking_findings) > 0,
        }

    @classmethod
    def execute_post_commit_ingest(cls, repo_root: Optional[Path] = None, silent: bool = False) -> bool:
        """
        [COGNEE PRIMITIVE: remember]
        Auto-ingests the just-committed changes into Cognee knowledge graph.

        Runs silently after every git commit so codebase memory stays current
        without requiring manual 'mnemosyne remember' commands.
        """
        root = repo_root or find_project_root()
        client = MemoryClient(root)

        try:
            commits = GitInspector.get_commit_history(root, max_commits=1)
            if commits:
                commit = commits[0]
                client.remember_commit(commit)
                if not silent:
                    logger.info(f"Auto-ingested commit {commit.commit_hash[:7]} into Cognee")
            return True
        except Exception as e:
            if not silent:
                logger.debug(f"Post-commit ingest failed: {e}")
            return False
