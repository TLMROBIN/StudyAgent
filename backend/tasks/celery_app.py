import logging

from celery import Celery
from celery.signals import worker_ready

from backend.config import get_settings
from backend.services.gpu_runtime import log_gpu_runtime_status

settings = get_settings()
logger = logging.getLogger(__name__)

celery_app = Celery(
    "studyagent",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["backend.tasks.ingest"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=False,
    task_track_started=True,
    worker_max_tasks_per_child=1,
    worker_prefetch_multiplier=1,
)


@worker_ready.connect
def _log_worker_runtime_status(**_: object) -> None:
    requested_device = settings.mineru_device if settings.pdf_parser_backend == "mineru" else None
    python_bin = settings.mineru_python_bin if settings.pdf_parser_backend == "mineru" else None
    log_gpu_runtime_status("worker", requested_device=requested_device, python_bin=python_bin)
    logger.info(
        "Worker runtime initialized | pdf_parser_backend=%s mineru_device=%s mineru_python_bin=%s",
        settings.pdf_parser_backend,
        settings.mineru_device,
        settings.mineru_python_bin,
    )
