from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from assignment_config import AssignmentFileConfig


@dataclass(frozen=True)
class HookRuntime:
    project_root: Path
    hooks_dir: Path
    mounts: dict[str, list[Path]]

    @classmethod
    def from_config(
        cls,
        cfg: AssignmentFileConfig,
        *,
        assignment_config_path: Path,
    ) -> HookRuntime | None:
        if not cfg.hooks.mounts:
            return None

        project_root = assignment_config_path.parents[2].resolve()
        hooks_dir = (project_root / cfg.hooks.dir).resolve()

        mounts: dict[str, list[Path]] = {}
        for mount_point, script_cfg in cfg.hooks.mounts.items():
            script_rels = [script_cfg] if isinstance(script_cfg, str) else script_cfg
            script_paths: list[Path] = []
            for script_rel in script_rels:
                script_path = (hooks_dir / script_rel).resolve()
                if not script_path.exists():
                    msg = (
                        f"Hook script not found for mount point '{mount_point}': {script_path}\n"
                        f"Expected under hooks dir: {hooks_dir}"
                    )
                    raise FileNotFoundError(msg)
                script_paths.append(script_path)
            mounts[str(mount_point)] = script_paths

        return cls(project_root=project_root, hooks_dir=hooks_dir, mounts=mounts)

    def run(self, mount_point: str, payload: dict[str, Any]) -> dict[str, Any]:
        script_paths = self.mounts.get(mount_point)
        if script_paths is None:
            return payload

        current_payload = payload
        for script_path in script_paths:
            current_payload = self._run_single_hook(
                mount_point=mount_point,
                script_path=script_path,
                payload=current_payload,
            )

        return current_payload

    def _run_single_hook(
        self,
        *,
        mount_point: str,
        script_path: Path,
        payload: dict[str, Any],
    ) -> dict[str, Any]:

        env = os.environ.copy()
        env["TATA_HOOK_MOUNT_POINT"] = mount_point
        env["TATA_HOOK_PROJECT_ROOT"] = str(self.project_root)

        proc = subprocess.run(
            [sys.executable, str(script_path)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

        if proc.returncode != 0:
            msg = (
                f"Hook failed at mount point '{mount_point}' using {script_path}\n"
                f"stderr: {proc.stderr.strip() or '<empty>'}"
            )
            raise RuntimeError(msg)

        output = proc.stdout.strip()
        if not output:
            return payload

        try:
            parsed = json.loads(output)
        except json.JSONDecodeError as exc:
            msg = (
                f"Hook '{script_path}' returned invalid JSON at '{mount_point}'.\n"
                f"stdout: {output[:400]}"
            )
            raise RuntimeError(msg) from exc

        if not isinstance(parsed, dict):
            msg = (
                f"Hook '{script_path}' must return a JSON object at '{mount_point}', "
                f"got {type(parsed).__name__}."
            )
            raise RuntimeError(msg)

        return parsed
