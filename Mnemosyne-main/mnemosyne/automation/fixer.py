"""
Mnemosyne Automation - AutoFixEngine
Implements the 3-step hierarchical bug correction pipeline:
1. Deterministic code transformations (safe lint, unused imports, bare excepts, insecure defaults)
2. Historical fix retrieval and pattern application
3. AI-assisted patch generation via Ollama/LLM with strict bounds and validation
Preserves original code, generates unified diffs, and isolates changes in WorkspaceManager.
"""
import re
import ast
import json
import logging
import urllib.request
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any
from mnemosyne.automation.models import (
    ScanFinding, PatchDiff, Severity, FixEligibility, DetectorSource
)
from mnemosyne.automation.workspace import WorkspaceManager
from mnemosyne.memory.client import MemoryClient

logger = logging.getLogger(__name__)


class AutoFixEngine:
    """Central engine for generating and applying code corrections."""

    def __init__(self, workspace: WorkspaceManager, memory_client: Optional[MemoryClient] = None, repo_root: Optional[Path] = None):
        self.workspace = workspace
        self.repo_root = repo_root or Path.cwd()
        self.memory_client = memory_client

    def apply_fixes(self, findings: List[ScanFinding], file_path: str) -> Optional[PatchDiff]:
        """Apply deterministic, historical, or AI fixes to a single file in the workspace."""
        try:
            content = self.workspace.read_file(file_path)
        except Exception:
            return None

        original = content
        modified = content
        fix_type = "none"

        # Step 1: Apply Deterministic Fixes
        deterministic_modified, applied_fixes = self._apply_deterministic_fixes(modified, findings)
        if deterministic_modified != modified:
            modified = deterministic_modified
            fix_type = "deterministic"
            for f in findings:
                if any(rule in f.rule_id for rule in ["BARE-EXCEPT", "UNUSED-IMPORT", "SAFE-LOAD", "UNSAFE-YAML"]):
                    f.applied_fix = "Applied deterministic safety transformation"

        # Step 2: Apply Historical Fixes if remaining fixable findings exist
        unfixed = [f for f in findings if not f.applied_fix and f.fix_eligibility == FixEligibility.AUTO_FIXABLE]
        if unfixed and self.memory_client:
            historical_modified, hist_fixes = self._apply_historical_fixes(modified, unfixed, file_path)
            if historical_modified != modified:
                modified = historical_modified
                fix_type = "historical_pattern" if fix_type == "none" else "hybrid"

        # Step 3: AI Patch Generation for remaining eligible bugs
        still_unfixed = [f for f in findings if not f.applied_fix and f.fix_eligibility == FixEligibility.AUTO_FIXABLE]
        if still_unfixed and self._is_ai_available():
            ai_modified = self._generate_ai_patch(modified, still_unfixed, file_path)
            if ai_modified and ai_modified != modified:
                # Basic sanity check: ensure modified python code parses
                if file_path.endswith(".py"):
                    try:
                        ast.parse(ai_modified)
                        modified = ai_modified
                        fix_type = "ai_patch" if fix_type == "none" else "hybrid"
                        for f in still_unfixed:
                            f.applied_fix = "Applied AI-generated patch"
                    except SyntaxError:
                        logger.warning("AI patch introduced syntax error; rejecting candidate")
                else:
                    modified = ai_modified
                    fix_type = "ai_patch"

        if modified != original:
            self.workspace.write_file(file_path, modified)
            patch = self.workspace.compute_diff(file_path)
            patch.fix_type = fix_type
            return patch

        return None

    # -------------------------------------------------------------------------
    # Step 1: Deterministic Transformations
    # -------------------------------------------------------------------------
    def _apply_deterministic_fixes(self, content: str, findings: List[ScanFinding]) -> Tuple[str, List[str]]:
        lines = content.splitlines(keepends=True)
        applied = []

        # 1. Fix bare excepts: 'except:' -> 'except Exception:'
        new_lines = []
        for line in lines:
            bare_match = re.match(r'^(\s*)except\s*:\s*(#.*)?$', line)
            if bare_match:
                indent = bare_match.group(1)
                comment = f" {bare_match.group(2)}" if bare_match.group(2) else ""
                new_lines.append(f"{indent}except Exception:{comment}\n")
                applied.append("Fixed bare except")
            else:
                new_lines.append(line)
        lines = new_lines

        # 2. Fix yaml.load(data) -> yaml.safe_load(data)
        new_lines = []
        for line in lines:
            if "yaml.load(" in line and "Loader" not in line:
                fixed_line = line.replace("yaml.load(", "yaml.safe_load(")
                new_lines.append(fixed_line)
                applied.append("Replaced yaml.load with yaml.safe_load")
            else:
                new_lines.append(line)
        lines = new_lines

        # 3. Fix unused imports: remove single unused import lines
        unused_import_findings = [f for f in findings if f.rule_id == "PY-UNUSED-IMPORT" and f.line_number]
        if unused_import_findings:
            lines_to_remove = set()
            for f in unused_import_findings:
                if f.line_number and 1 <= f.line_number <= len(lines):
                    target_line = lines[f.line_number - 1].strip()
                    # Only remove if it's a simple import statement on its own line
                    if target_line.startswith("import ") or target_line.startswith("from "):
                        lines_to_remove.add(f.line_number - 1)
                        applied.append(f"Removed unused import at line {f.line_number}")

            if lines_to_remove:
                lines = [line for idx, line in enumerate(lines) if idx not in lines_to_remove]

        return "".join(lines), applied

    # -------------------------------------------------------------------------
    # Step 2: Historical Fix Application
    # -------------------------------------------------------------------------
    def _apply_historical_fixes(self, content: str, findings: List[ScanFinding], file_path: str) -> Tuple[str, List[str]]:
        applied = []
        # Query past memories linked to these findings
        for f in findings:
            if not f.matching_memory_ids:
                continue
            for mem_id in f.matching_memory_ids:
                rec = self.memory_client.get_memory(mem_id)
                if not rec:
                    continue
                # If memory has exact root cause & fix pattern, see if applicable
                fix_desc = rec.metadata.get("fix_description", "")
                if "replace shell=True" in fix_desc.lower() or "shell=false" in fix_desc.lower():
                    if "shell=True" in content:
                        content = content.replace("shell=True", "shell=False")
                        f.applied_fix = f"Applied historical pattern from {mem_id}"
                        applied.append(mem_id)
        return content, applied

    # -------------------------------------------------------------------------
    # Step 3: AI-Assisted Patch Generation
    # -------------------------------------------------------------------------
    def _is_ai_available(self) -> bool:
        if os.getenv("MNEMOSYNE_OFFLINE") == "1":
            return False
        from mnemosyne.config import load_config
        config = load_config(self.repo_root)
        provider = config.get("llm_provider", "ollama")
        if provider == "ollama":
            import urllib.request
            try:
                base_url = config.get("ollama_base_url", "http://localhost:11434/v1")
                check_url = base_url.replace("/v1", "") + "/api/tags"
                req = urllib.request.Request(check_url, headers={"User-Agent": "Mnemosyne"})
                with urllib.request.urlopen(req, timeout=1) as res:
                    return res.status == 200
            except Exception:
                return False
        return bool(os.getenv("OPENAI_API_KEY") or config.get("llm_api_key"))

    def _generate_ai_patch(self, content: str, findings: List[ScanFinding], file_path: str) -> Optional[str]:
        """Request LLM candidate patch for unresolved findings."""
        from mnemosyne.config import load_config
        config = load_config(self.repo_root)
        model = config.get("llm_model", "llama3.1:8b")
        base_url = config.get("ollama_base_url", "http://localhost:11434/v1")

        issues_summary = "\n".join([
            f"- Line {f.line_number}: [{f.rule_id}] {f.description} (Suggested fix: {f.suggested_fix})"
            for f in findings
        ])

        prompt = f"""You are an automated software engineer repairing a file.
File: {file_path}

Issues detected:
{issues_summary}

Original Code:
```
{content}
```

Instructions:
1. Apply minimal, precise, surgical fixes for the issues detected.
2. Do NOT change unrelated logic or public APIs.
3. Return the COMPLETE repaired file content inside a single ``` codeblock.
4. Output nothing else except the codeblock.
"""

        try:
            url = f"{base_url}/chat/completions"
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "You are an automated code repair assistant. Output only the repaired code inside a single markdown codeblock."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.1,
                "max_tokens": 3000,
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as res:
                res_data = json.loads(res.read().decode("utf-8"))
                text = res_data["choices"][0]["message"]["content"]
                # Extract code between triple backticks
                match = re.search(r'```(?:[a-zA-Z]+\n)?(.*?)```', text, re.DOTALL)
                if match:
                    repaired_code = match.group(1).rstrip() + "\n"
                    return repaired_code
        except Exception as e:
            logger.debug(f"AI patch generation failed: {e}")
        return None
