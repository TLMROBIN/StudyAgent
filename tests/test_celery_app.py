from backend.tasks.celery_app import celery_app


def test_celery_app_registers_ingest_task():
    celery_app.loader.import_default_modules()

    assert "backend.tasks.ingest.ingest_document_task" in celery_app.tasks
