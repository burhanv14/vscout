import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Type, Union
from pydantic import BaseModel, ValidationError

from oss_agent.config import AppConfig, get_project_root

logger = logging.getLogger("oss_agent.brain")


class BrainExecutionError(Exception):
    """Raised when opencode execution fails or cannot be validated."""
    pass


class LLMBrain:
    """Core brain driving all reasoning via the local opencode CLI."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.root = get_project_root()
        self.log_file = self.root / "logs" / "brain_calls.jsonl"
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def _find_opencode_binary(self) -> str:
        if self.config.opencode.opencode_path and os.path.exists(self.config.opencode.opencode_path):
            return str(Path(self.config.opencode.opencode_path).resolve())

        which_bin = shutil.which("opencode")
        if which_bin:
            return which_bin

        # Check standard Windows npm and AppData locations
        appdata = os.environ.get("APPDATA", "")
        localappdata = os.environ.get("LOCALAPPDATA", "")
        userprofile = os.environ.get("USERPROFILE", "")

        candidates = [
            Path(appdata) / "npm" / "opencode.cmd",
            Path(localappdata) / "pnpm" / "opencode.cmd",
            Path(userprofile) / ".local" / "bin" / "opencode.exe",
        ]
        for cand in candidates:
            if cand.exists():
                return str(cand)

        return "opencode"

    def _build_command(self, prompt: str) -> list[str]:
        opencode_bin = self._find_opencode_binary()
        cmd = [opencode_bin, "run", prompt, "--format", "json"]

        # Server backend check
        if self.config.opencode.backend == "server" and self.config.opencode.server_url:
            cmd.extend(["--server", self.config.opencode.server_url])

        # Optional model flag
        if self.config.opencode.model:
            cmd.extend(["--model", self.config.opencode.model])

        # Auto-approve actions for non-interactive execution
        cmd.append("--auto")
        return cmd

    def _execute_subprocess(
        self, cmd: list[str], cwd: Optional[Path], timeout: int
    ) -> tuple[int, str, str]:
        work_dir = str(cwd) if cwd else str(self.root)
        try:
            # On Windows, running .cmd or .bat files requires shell=True
            is_windows = os.name == "nt"
            use_shell = is_windows and cmd[0].lower().endswith((".cmd", ".bat"))
            result = subprocess.run(
                cmd,
                cwd=work_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=use_shell,
                timeout=timeout,
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired as exc:
            raise BrainExecutionError(
                f"opencode execution timed out after {timeout} seconds"
            ) from exc
        except FileNotFoundError as exc:
            raise BrainExecutionError(
                "opencode CLI executable was not found on PATH. Make sure opencode is installed."
            ) from exc

    def _extract_text_from_opencode_output(self, stdout: str) -> str:
        """
        Extract the generated message text from opencode output.
        opencode --format json may output lines of JSON events or a single JSON payload.
        """
        if not stdout.strip():
            return ""

        collected_texts = []
        for line in stdout.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            try:
                data = json.loads(line_str)
                if isinstance(data, dict):
                    # Check common opencode json event structures
                    if "text" in data:
                        collected_texts.append(data["text"])
                    elif "content" in data and isinstance(data["content"], str):
                        collected_texts.append(data["content"])
                    elif "part" in data and isinstance(data["part"], dict) and "text" in data["part"]:
                        collected_texts.append(data["part"]["text"])
                    elif "message" in data and isinstance(data["message"], dict) and "content" in data["message"]:
                        collected_texts.append(str(data["message"]["content"]))
                    elif "response" in data:
                        collected_texts.append(str(data["response"]))
            except json.JSONDecodeError:
                continue

        if collected_texts:
            return "".join(collected_texts)

        # If not JSON-lines, return raw stdout
        return stdout

    def _extract_json_block(self, text: str) -> str:
        """Find and extract a JSON code block or bare JSON object."""
        # Check for ```json ... ``` markdown code block
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
        if match:
            return match.group(1).strip()

        # Find first { and last }
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            return text[first_brace : last_brace + 1].strip()

        return text.strip()

    def _log_call(
        self,
        *,
        prompt: str,
        duration: float,
        schema_name: Optional[str],
        attempts: int,
        success: bool,
        error: Optional[str] = None,
    ) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": round(duration, 3),
            "schema": schema_name,
            "attempts": attempts,
            "success": success,
            "model": self.config.opencode.model or "default",
            "backend": self.config.opencode.backend,
            "prompt_length": len(prompt),
            "prompt_preview": prompt[:200].replace("\n", " "),
            "error": error,
        }
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as log_err:
            logger.warning(f"Failed to append to brain log: {log_err}")

    def think(
        self,
        prompt: str,
        *,
        cwd: Optional[Union[str, Path]] = None,
        schema: Optional[Type[BaseModel]] = None,
    ) -> Union[str, Dict[str, Any], BaseModel]:
        """
        Execute reasoning task with opencode CLI.
        Forces structured JSON output and validates against schema with 3x retry on failure.
        """
        cwd_path = Path(cwd).resolve() if cwd else None
        max_retries = max(1, self.config.opencode.max_retries)
        timeout = self.config.opencode.timeout_seconds
        schema_name = schema.__name__ if schema else None

        current_prompt = prompt
        if schema:
            schema_json = json.dumps(schema.model_json_schema(), indent=2)
            current_prompt += (
                f"\n\nCRITICAL REQUIREMENT: Output MUST be strictly valid JSON matching this schema:\n"
                f"```json\n{schema_json}\n```\n"
                f"Do not include any commentary before or after the JSON block."
            )

        start_time = time.time()
        last_error = None

        for attempt in range(1, max_retries + 1):
            cmd = self._build_command(current_prompt)
            try:
                code, stdout, stderr = self._execute_subprocess(cmd, cwd_path, timeout)
                if code != 0 and not stdout.strip():
                    raise BrainExecutionError(
                        f"opencode exited with code {code}: {stderr.strip()}"
                    )

                raw_output = self._extract_text_from_opencode_output(stdout)
                if not raw_output:
                    raw_output = stdout.strip()

                if not schema:
                    self._log_call(
                        prompt=prompt,
                        duration=time.time() - start_time,
                        schema_name=None,
                        attempts=attempt,
                        success=True,
                    )
                    return raw_output

                # Schema is specified, parse and validate JSON
                json_candidate = self._extract_json_block(raw_output)
                parsed_dict = json.loads(json_candidate)
                validated_model = schema.model_validate(parsed_dict)

                self._log_call(
                    prompt=prompt,
                    duration=time.time() - start_time,
                    schema_name=schema_name,
                    attempts=attempt,
                    success=True,
                )
                return validated_model

            except (json.JSONDecodeError, ValidationError, BrainExecutionError) as err:
                last_error = str(err)
                logger.warning(
                    f"Brain think attempt {attempt}/{max_retries} failed for schema {schema_name}: {last_error}"
                )

                if attempt < max_retries:
                    current_prompt += (
                        f"\n\n[VALIDATION RETRY {attempt}/{max_retries}]: "
                        f"Your previous response produced this error: {last_error}\n"
                        f"Please fix the error and output ONLY valid JSON adhering strictly to the schema."
                    )
                else:
                    self._log_call(
                        prompt=prompt,
                        duration=time.time() - start_time,
                        schema_name=schema_name,
                        attempts=attempt,
                        success=False,
                        error=last_error,
                    )
                    raise BrainExecutionError(
                        f"LLMBrain failed after {max_retries} attempts: {last_error}"
                    ) from err

        raise BrainExecutionError(f"LLMBrain failed: {last_error}")
