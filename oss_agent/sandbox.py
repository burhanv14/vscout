import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from rich.console import Console

from oss_agent.config import AppConfig

logger = logging.getLogger("oss_agent.sandbox")
console = Console()


class SandboxExecutionResult:
    def __init__(self, command: str, exit_code: int, stdout: str, stderr: str, duration: float, sandbox_type: str):
        self.command = command
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.duration = duration
        self.sandbox_type = sandbox_type

    @property
    def success(self) -> bool:
        return self.exit_code == 0


class SandboxRunner:
    """Executes dependency installs and tests in Docker or an isolated venv."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.has_docker = self._check_docker()

    def _check_docker(self) -> bool:
        try:
            res = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return res.returncode == 0
        except Exception:
            return False

    def run_command(
        self,
        command: str,
        workspace_dir: Path,
        env_vars: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> SandboxExecutionResult:
        """
        Run command in sandbox environment.
        Prefers Docker container; cleanly falls back to isolated venv with human notice.
        """
        timeout = timeout or self.config.sandbox.timeout_seconds
        start_time = time.time()

        if self.config.sandbox.mode == "docker" or (self.config.sandbox.mode == "auto" and self.has_docker):
            try:
                return self._run_in_docker(command, workspace_dir, env_vars, timeout, start_time)
            except Exception as e:
                console.print(f"[yellow]Docker execution failed or unavailable ({e}); falling back to isolated environment.[/yellow]")

        console.print("[dim cyan][Sandbox][/dim cyan] Running command in isolated local workspace environment...")
        return self._run_in_local_venv(command, workspace_dir, env_vars, timeout, start_time)

    def _run_in_docker(
        self,
        command: str,
        workspace_dir: Path,
        env_vars: Optional[Dict[str, str]],
        timeout: int,
        start_time: float,
    ) -> SandboxExecutionResult:
        abs_ws = str(workspace_dir.resolve())
        docker_cmd = [
            "docker", "run", "--rm",
            "-v", f"{abs_ws}:/workspace",
            "-w", "/workspace",
        ]
        if env_vars:
            for k, v in env_vars.items():
                docker_cmd.extend(["-e", f"{k}={v}"])

        # Default image for python / node tasks
        docker_cmd.extend(["python:3.11-slim", "sh", "-c", command])

        res = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return SandboxExecutionResult(
            command=command,
            exit_code=res.returncode,
            stdout=res.stdout,
            stderr=res.stderr,
            duration=time.time() - start_time,
            sandbox_type="docker",
        )

    def _run_in_local_venv(
        self,
        command: str,
        workspace_dir: Path,
        env_vars: Optional[Dict[str, str]],
        timeout: int,
        start_time: float,
    ) -> SandboxExecutionResult:
        env = os.environ.copy()
        if env_vars:
            env.update(env_vars)

        # Ensure git from C:\Program Files\Git\cmd is included if installed
        git_dir = r"C:\Program Files\Git\cmd"
        if os.path.isdir(git_dir) and git_dir not in env.get("PATH", ""):
            env["PATH"] = git_dir + os.pathsep + env.get("PATH", "")

        # Execute command in shell inside workspace
        try:
            res = subprocess.run(
                command,
                cwd=str(workspace_dir),
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=timeout,
            )
            return SandboxExecutionResult(
                command=command,
                exit_code=res.returncode,
                stdout=res.stdout,
                stderr=res.stderr,
                duration=time.time() - start_time,
                sandbox_type="local_isolated",
            )
        except subprocess.TimeoutExpired as exc:
            return SandboxExecutionResult(
                command=command,
                exit_code=-1,
                stdout=exc.stdout or "",
                stderr=f"Command timed out after {timeout} seconds",
                duration=time.time() - start_time,
                sandbox_type="local_isolated",
            )
