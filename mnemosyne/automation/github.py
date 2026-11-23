"""
Mnemosyne Automation - GitHub Integration Manager
Handles:
1. Automation branch creation (mnemosyne/autofix/<job-id>)
2. Committing verified patches with descriptive changelogs
3. Safe push to authorized origin
4. Pull Request creation via GitHub REST API with structured markdown report
5. Safe fallback and audit trail when GitHub tokens or remotes are not configured
"""
import os
import re
import json
import logging
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, Dict, Any, List
from mnemosyne.automation.models import (
    GitHubPRResult, PatchDiff, VerificationResult, ScanFinding
)
from mnemosyne.git.inspector import GitInspector

logger = logging.getLogger(__name__)


class GitHubManager:
    """Manages git branches, commits, pushes, and Pull Requests for automation jobs."""

    def __init__(self, repo_root: Optional[Path] = None, token: Optional[str] = None):
        self.repo_root = repo_root or Path.cwd()
        self.token = token or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
        self.repo = GitInspector.get_repo(self.repo_root)

    def get_repo_slug(self) -> Optional[str]:
        """Extract owner/repo from git remotes or environment."""
        env_slug = os.getenv("GITHUB_REPOSITORY")
        if env_slug:
            return env_slug

        if not self.repo:
            return "kanikachaudhary70/Mnemosyne"

        try:
            for remote in self.repo.remotes:
                for url in remote.urls:
                    # Match https://github.com/owner/repo.git or git@github.com:owner/repo.git
                    m = re.search(r'github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?$', url)
                    if m:
                        return f"{m.group(1)}/{m.group(2)}"
        except Exception:
            pass
        return "kanikachaudhary70/Mnemosyne"

    def publish_verified_fixes(
        self,
        job_id: str,
        patches: List[PatchDiff],
        findings: List[ScanFinding],
        verification: VerificationResult,
        target_branch: str = "main",
    ) -> GitHubPRResult:
        """Create dedicated branch, commit changes, push, and open a Pull Request."""
        result = GitHubPRResult(status="pending")
        branch_name = f"mnemosyne/autofix-{job_id[:8]}"
        result.branch_name = branch_name

        if not self.repo:
            result.status = "error"
            result.error_message = "Git repository not found on server."
            return result

        # 1. Create or checkout automation branch
        try:
            current_head = self.repo.active_branch.name
            # Create new branch from current head
            new_branch = self.repo.create_head(branch_name)
            new_branch.checkout()

            # 2. Apply patches to actual repository files
            modified_files = []
            for patch in patches:
                target_file = (self.repo_root / patch.file_path).resolve()
                target_file.parent.mkdir(parents=True, exist_ok=True)
                target_file.write_text(patch.patched_content, encoding="utf-8")
                self.repo.index.add([str(target_file)])
                modified_files.append(patch.file_path)

            # 3. Commit
            commit_msg = (
                f"fix: resolve validated issues detected by Mnemosyne [job: {job_id[:8]}]\n\n"
                f"- Resolved {len(verification.resolved_finding_ids)} findings\n"
                f"- Verified: {verification.status.value}\n"
                f"- Modified files: {', '.join(modified_files)}"
            )
            commit = self.repo.index.commit(commit_msg)
            result.commit_hash = commit.hexsha

            # Return to previous branch locally so working directory is clean
            try:
                self.repo.heads[current_head].checkout()
            except Exception:
                pass

        except Exception as e:
            result.status = "error"
            result.error_message = f"Failed to create git branch/commit: {e}"
            return result

        # 4. Check for GitHub Token before pushing / PR
        if not self.token:
            result.status = "auth_required"
            result.error_message = (
                f"Local commit created ({result.commit_hash[:7]}). "
                "GitHub token (GITHUB_TOKEN or GH_TOKEN) is not configured; "
                "push and pull request creation skipped."
            )
            return result

        # 5. Push branch to remote
        try:
            origin = self.repo.remotes.origin
            origin.push(refspec=f"{branch_name}:{branch_name}")
        except Exception as e:
            result.status = "push_failed"
            result.error_message = f"Failed to push branch to GitHub remote: {e}"
            return result

        # 6. Open Pull Request via GitHub API
        slug = self.get_repo_slug()
        if not slug:
            result.status = "error"
            result.error_message = "Could not resolve GitHub repository slug (owner/repo)."
            return result

        pr_title = f"fix(mnemosyne): automated bug fixes for job {job_id[:8]}"
        pr_body = self._build_pr_body(job_id, patches, findings, verification)

        try:
            pr_data = self._create_pull_request(slug, branch_name, target_branch, pr_title, pr_body)
            result.pr_number = pr_data.get("number")
            result.pr_url = pr_data.get("html_url")
            result.status = "published"
        except urllib.error.HTTPError as e:
            err_text = e.read().decode("utf-8", errors="replace")
            result.status = "api_error"
            result.error_message = f"GitHub API error {e.code}: {err_text}"
        except Exception as e:
            result.status = "api_error"
            result.error_message = f"PR creation failed: {e}"

        return result

    def _create_pull_request(self, repo_slug: str, head_branch: str, base_branch: str, title: str, body: str) -> Dict[str, Any]:
        """POST to GitHub API /repos/{owner}/{repo}/pulls."""
        api_url = f"https://api.github.com/repos/{repo_slug}/pulls"
        headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "Mnemosyne-Automated-Fixer",
            "Content-Type": "application/json",
        }
        payload = {
            "title": title,
            "head": head_branch,
            "base": base_branch,
            "body": body,
        }
        req = urllib.request.Request(api_url, data=json.dumps(payload).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req, timeout=15) as res:
            return json.loads(res.read().decode("utf-8"))

    def _build_pr_body(
        self,
        job_id: str,
        patches: List[PatchDiff],
        findings: List[ScanFinding],
        verification: VerificationResult
    ) -> str:
        """Generate comprehensive PR description."""
        resolved = [f for f in findings if f.finding_id in verification.resolved_finding_ids]
        remaining = [f for f in findings if f.finding_id not in verification.resolved_finding_ids]

        body = [
            "## 🧠 Mnemosyne Autonomous Bug Detection & Auto-Fix Report",
            f"**Automation Job ID:** `{job_id}`  ",
            f"**Verification Outcome:** `{verification.status.value.upper()}`  ",
            "",
            "### 📋 Summary of Findings",
            f"- **Total Issues Scanned:** {len(findings)}",
            f"- **Automatically Resolved & Verified:** {len(resolved)}",
            f"- **Remaining for Manual Review:** {len(remaining)}",
            "",
            "### 🛠️ Resolved Issues",
        ]

        if resolved:
            for r in resolved:
                body.append(f"- **[{r.severity.value.upper()}]** `{r.file_path}:{r.line_number or 1}` — {r.description}")
                if r.matching_memory_ids:
                    body.append(f"  - *Associated Memory ID(s):* `{', '.join(r.matching_memory_ids)}`")
        else:
            body.append("*No automated fixes were applied.*")

        if remaining:
            body.extend([
                "",
                "### ⚠️ Items Requiring Human Review",
            ])
            for rem in remaining:
                body.append(f"- **[{rem.severity.value.upper()}]** `{rem.file_path}:{rem.line_number or 1}` — {rem.description}")
                if rem.suggested_fix:
                    body.append(f"  - *Guidance:* {rem.suggested_fix}")

        body.extend([
            "",
            "### 🧪 Verification Details",
            f"- **Checks Executed:** `{', '.join(verification.checks_run)}`",
            f"- **Syntax Valid:** `{'✓ Yes' if verification.syntax_valid else '✗ No'}`",
            f"- **Security Clean:** `{'✓ Yes' if verification.security_clean else '✗ No'}`",
            f"- **Automated Tests:** `{'✓ Passed' if verification.tests_passed else '✗ Failed'}`",
            "",
            "---",
            "*Automated Pull Request generated by Mnemosyne Codebase Memory & Verification Engine.*",
        ])

        return "\n".join(body)
