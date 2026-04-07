from backend.services import gpu_runtime
from backend.services.mineru_service import MineruService
from tests.test_rag import build_rag_service


def test_collect_cuda_requirement_snapshot_reports_not_ready_when_cuda_requested_but_runtime_missing(monkeypatch):
    monkeypatch.setattr(
        gpu_runtime,
        "collect_gpu_runtime_snapshot",
        lambda *, python_bin=None: {
            "ready": False,
            "mineru": {"installed": True, "error": None},
            "torch": {"cuda_available": False, "device_names": [], "error": "cuda unavailable"},
            "nvidia_smi": {"ok": False, "error": "nvml error", "gpus": []},
            "env": {"cuda_visible_devices": None, "nvidia_visible_devices": "all"},
        },
    )

    snapshot = gpu_runtime.collect_cuda_requirement_snapshot("cuda")

    assert snapshot["requested"] is True
    assert snapshot["ready"] is False
    assert snapshot["runtime_ready"] is False
    assert snapshot["torch"]["cuda_available"] is False
    assert snapshot["nvidia_smi"]["ok"] is False


def test_collect_cuda_requirement_snapshot_passes_python_bin_to_probe(monkeypatch):
    recorded: dict[str, str | None] = {}

    def fake_collect(*, python_bin=None):
        recorded["python_bin"] = python_bin
        return {
            "ready": True,
            "mineru": {"installed": True, "error": None},
            "torch": {"cuda_available": True, "device_names": ["GPU"], "error": None},
            "nvidia_smi": {"ok": True, "error": None, "gpus": [{"name": "GPU"}]},
            "env": {"cuda_visible_devices": "0", "nvidia_visible_devices": "all"},
        }

    monkeypatch.setattr(gpu_runtime, "collect_gpu_runtime_snapshot", fake_collect)

    snapshot = gpu_runtime.collect_cuda_requirement_snapshot("cuda", python_bin="/tmp/mineru-venv/bin/python")

    assert recorded["python_bin"] == "/tmp/mineru-venv/bin/python"
    assert snapshot["runtime_ready"] is True


def test_mineru_health_snapshot_exposes_gpu_state(tmp_path, monkeypatch):
    service = MineruService()
    service.settings.pdf_parser_backend = "mineru"
    service.settings.mineru_device = "cuda"
    service.settings.mineru_python_bin = "/usr/local/bin/python"

    monkeypatch.setattr(
        "backend.services.mineru_service.collect_cuda_requirement_snapshot",
        lambda requested_device, *, python_bin=None: {
            "requested": True,
            "ready": True,
            "runtime_ready": True,
            "configured_device": requested_device,
            "python_bin": python_bin,
            "mineru": {"installed": True, "error": None},
            "torch": {"cuda_available": True},
            "nvidia_smi": {"ok": True},
            "env": {},
        },
    )

    snapshot = service.health_snapshot()

    assert snapshot["enabled"] is True
    assert snapshot["configured_backend"] == "mineru"
    assert snapshot["configured_device"] == "cuda"
    assert snapshot["runtime_ready"] is True
    assert snapshot["mineru"]["installed"] is True
    assert snapshot["gpu"]["ready"] is True


def test_rag_health_snapshot_includes_pdf_parser(tmp_path, monkeypatch):
    rag_service = build_rag_service(tmp_path)
    monkeypatch.setattr(
        "backend.services.rag_service.mineru_service.health_snapshot",
        lambda: {"enabled": True, "gpu": {"requested": True, "ready": True}},
    )

    snapshot = rag_service.health_snapshot()

    assert snapshot["pdf_parser"]["enabled"] is True
    assert snapshot["pdf_parser"]["gpu"]["ready"] is True
