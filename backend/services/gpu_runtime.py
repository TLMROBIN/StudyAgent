from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

_PYTHON_RUNTIME_PROBE = """
import importlib.util
import json

payload = {
    "python_ok": True,
    "mineru_installed": False,
    "mineru_error": None,
    "torch_installed": False,
    "torch_version": None,
    "cuda_available": False,
    "device_count": 0,
    "device_names": [],
    "error": None,
}

try:
    payload["mineru_installed"] = importlib.util.find_spec("mineru") is not None
except Exception as exc:
    payload["mineru_error"] = str(exc)

try:
    import torch

    payload["torch_installed"] = True
    payload["torch_version"] = getattr(torch, "__version__", None)
    payload["cuda_available"] = bool(torch.cuda.is_available())
    payload["device_count"] = int(torch.cuda.device_count()) if payload["cuda_available"] else 0
    payload["device_names"] = [torch.cuda.get_device_name(index) for index in range(payload["device_count"])]
except Exception as exc:
    payload["error"] = str(exc)

print(json.dumps(payload, ensure_ascii=False))
""".strip()


def _nvidia_smi_snapshot() -> dict[str, Any]:
    command = [
        "nvidia-smi",
        "--query-gpu=name,driver_version,memory.total,memory.used",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "gpus": []}

    if result.returncode != 0:
        return {"ok": False, "error": (result.stderr or result.stdout).strip() or f"exit={result.returncode}", "gpus": []}

    gpus: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 4:
            continue
        name, driver_version, memory_total, memory_used = parts[:4]
        gpus.append(
            {
                "name": name,
                "driver_version": driver_version,
                "memory_total_mb": _safe_int(memory_total),
                "memory_used_mb": _safe_int(memory_used),
            }
        )
    return {"ok": True, "error": None, "gpus": gpus}


def _python_runtime_snapshot(python_bin: str | None = None) -> dict[str, Any]:
    if python_bin:
        command = [python_bin, "-c", _PYTHON_RUNTIME_PROBE]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
        except Exception as exc:
            return {
                "python_ok": False,
                "mineru_installed": False,
                "mineru_error": str(exc),
                "torch_installed": False,
                "torch_version": None,
                "cuda_available": False,
                "device_count": 0,
                "device_names": [],
                "error": str(exc),
            }
        if result.returncode != 0:
            return {
                "python_ok": False,
                "mineru_installed": False,
                "mineru_error": (result.stderr or result.stdout).strip() or f"exit={result.returncode}",
                "torch_installed": False,
                "torch_version": None,
                "cuda_available": False,
                "device_count": 0,
                "device_names": [],
                "error": (result.stderr or result.stdout).strip() or f"exit={result.returncode}",
            }
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {
                "python_ok": False,
                "mineru_installed": False,
                "mineru_error": f"invalid probe output from {python_bin}",
                "torch_installed": False,
                "torch_version": None,
                "cuda_available": False,
                "device_count": 0,
                "device_names": [],
                "error": f"invalid torch probe output from {python_bin}",
            }

    mineru_error = None
    try:
        import importlib.util

        mineru_installed = importlib.util.find_spec("mineru") is not None
    except Exception as exc:
        mineru_installed = False
        mineru_error = str(exc)

    try:
        import torch
    except Exception as exc:
        return {
            "python_ok": True,
            "mineru_installed": mineru_installed,
            "mineru_error": mineru_error,
            "torch_installed": False,
            "torch_version": None,
            "cuda_available": False,
            "device_count": 0,
            "device_names": [],
            "error": str(exc),
        }

    try:
        cuda_available = bool(torch.cuda.is_available())
        device_count = int(torch.cuda.device_count()) if cuda_available else 0
        device_names = [torch.cuda.get_device_name(index) for index in range(device_count)]
        return {
            "python_ok": True,
            "mineru_installed": mineru_installed,
            "mineru_error": mineru_error,
            "torch_installed": True,
            "torch_version": getattr(torch, "__version__", None),
            "cuda_available": cuda_available,
            "device_count": device_count,
            "device_names": device_names,
            "error": None,
        }
    except Exception as exc:
        return {
            "python_ok": True,
            "mineru_installed": mineru_installed,
            "mineru_error": mineru_error,
            "torch_installed": True,
            "torch_version": getattr(torch, "__version__", None),
            "cuda_available": False,
            "device_count": 0,
            "device_names": [],
            "error": str(exc),
        }


def collect_gpu_runtime_snapshot(*, python_bin: str | None = None) -> dict[str, Any]:
    python_snapshot = _python_runtime_snapshot(python_bin=python_bin)
    torch_snapshot = {
        "torch_installed": python_snapshot.get("torch_installed"),
        "torch_version": python_snapshot.get("torch_version"),
        "cuda_available": python_snapshot.get("cuda_available"),
        "device_count": python_snapshot.get("device_count"),
        "device_names": python_snapshot.get("device_names"),
        "error": python_snapshot.get("error"),
    }
    mineru_snapshot = {
        "installed": bool(python_snapshot.get("mineru_installed")),
        "error": python_snapshot.get("mineru_error"),
    }
    nvidia_smi_snapshot = _nvidia_smi_snapshot()
    ready = (
        bool(mineru_snapshot.get("installed"))
        and bool(torch_snapshot.get("cuda_available"))
        and bool(nvidia_smi_snapshot.get("ok"))
    )
    return {
        "ready": ready,
        "python_bin": python_bin,
        "python": {"ok": bool(python_snapshot.get("python_ok", True))},
        "mineru": mineru_snapshot,
        "torch": torch_snapshot,
        "nvidia_smi": nvidia_smi_snapshot,
        "env": {
            "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
            "nvidia_visible_devices": os.environ.get("NVIDIA_VISIBLE_DEVICES"),
        },
    }


def collect_cuda_requirement_snapshot(requested_device: str | None, *, python_bin: str | None = None) -> dict[str, Any]:
    runtime = collect_gpu_runtime_snapshot(python_bin=python_bin)
    requested = requested_device == "cuda"
    return {
        "requested": requested,
        "ready": (not requested) or runtime["ready"],
        "runtime_ready": bool(runtime.get("mineru", {}).get("installed")) and ((not requested) or runtime["ready"]),
        "configured_device": requested_device,
        **runtime,
    }


def log_gpu_runtime_status(component: str, *, requested_device: str | None = None, python_bin: str | None = None) -> None:
    snapshot = collect_cuda_requirement_snapshot(requested_device, python_bin=python_bin)
    level = logging.INFO if snapshot["ready"] else logging.WARNING
    logger.log(
        level,
        "%s GPU runtime status | requested=%s ready=%s python_bin=%s mineru_installed=%s torch_cuda=%s devices=%s nvidia_smi=%s error=%s",
        component,
        snapshot["requested"],
        snapshot["ready"],
        snapshot.get("python_bin"),
        snapshot.get("mineru", {}).get("installed"),
        snapshot["torch"].get("cuda_available"),
        snapshot["torch"].get("device_names") or [],
        snapshot["nvidia_smi"].get("ok"),
        snapshot["nvidia_smi"].get("error")
        or snapshot["torch"].get("error")
        or snapshot.get("mineru", {}).get("error"),
    )


def _safe_int(value: str) -> int | None:
    try:
        return int(value)
    except Exception:
        return None
