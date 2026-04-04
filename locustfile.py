from __future__ import annotations

import json
import os
from random import choice

from locust import HttpUser, between, task
from locust.exception import StopUser

DEFAULT_SUBJECT = os.getenv("LOCUST_CHAT_SUBJECT", "数学")
DEFAULT_CHAT_MESSAGES = [
    "已知函数单调递增，第一步应该从哪里开始分析？",
    "遇到解析几何题时，通常先找哪些已知条件？",
    "数列求通项时，我应该先判断哪一类方法？",
]


def _coerce_base_host(raw_host: str) -> str:
    host = raw_host.strip().rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    return host


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class StudyAgentUser(HttpUser):
    host = _coerce_base_host(os.getenv("LOCUST_HOST", "http://127.0.0.1:8001"))
    wait_time = between(
        float(os.getenv("LOCUST_WAIT_MIN_SECONDS", "1")),
        float(os.getenv("LOCUST_WAIT_MAX_SECONDS", "3")),
    )
    abstract = True

    def _login(self, path: str, payload: dict[str, str], *, label: str) -> dict[str, str]:
        with self.client.post(path, json=payload, name=label, catch_response=True) as response:
            if response.status_code != 200:
                response.failure(f"login failed: {response.status_code} {response.text}")
                raise StopUser()

            try:
                data = response.json()
            except json.JSONDecodeError as exc:
                response.failure(f"invalid login response: {exc}")
                raise StopUser() from exc

            response.success()
            return data

    def _authorized_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


class StaffDashboardUser(StudyAgentUser):
    weight = int(os.getenv("LOCUST_STAFF_WEIGHT", "1"))

    def on_start(self) -> None:
        username = os.getenv("LOCUST_ADMIN_USERNAME", "admin")
        password = os.getenv("LOCUST_ADMIN_PASSWORD", "StudyAgent123")
        data = self._login(
            "/api/auth/staff/login",
            {"username": username, "password": password},
            label="/api/auth/staff/login",
        )
        self.access_token = data["access_token"]

    @task(2)
    def health(self):
        self.client.get("/health", name="/health")

    @task(1)
    def me(self):
        self.client.get("/api/auth/me", headers=self._authorized_headers(), name="/api/auth/me")

    @task(2)
    def stats_overview(self):
        self.client.get("/api/stats/overview", headers=self._authorized_headers(), name="/api/stats/overview")

    @task(1)
    def stats_classes(self):
        self.client.get("/api/stats/classes", headers=self._authorized_headers(), name="/api/stats/classes")

    @task(1)
    def stats_portraits(self):
        self.client.get(
            "/api/stats/portraits?limit=12",
            headers=self._authorized_headers(),
            name="/api/stats/portraits",
        )

    @task(1)
    def audit_logs(self):
        self.client.get(
            "/api/admin/audit-logs?limit=50",
            headers=self._authorized_headers(),
            name="/api/admin/audit-logs",
        )


class StudentChatUser(StudyAgentUser):
    weight = int(os.getenv("LOCUST_STUDENT_WEIGHT", "3"))

    def on_start(self) -> None:
        student_no = os.getenv("LOCUST_STUDENT_NO", "20269999")
        password = os.getenv("LOCUST_STUDENT_PASSWORD", "Loadtest123")
        data = self._login(
            "/api/auth/student/login",
            {"student_no": student_no, "password": password},
            label="/api/auth/student/login",
        )
        self.access_token = data["access_token"]
        self.enable_stream = _env_flag("LOCUST_ENABLE_STREAM", True)
        self.subject = os.getenv("LOCUST_CHAT_SUBJECT", DEFAULT_SUBJECT)
        configured_messages = [
            item.strip()
            for item in os.getenv("LOCUST_CHAT_MESSAGES", "").split("|")
            if item.strip()
        ]
        self.messages = configured_messages or DEFAULT_CHAT_MESSAGES

    @task(1)
    def conversation_history(self):
        self.client.get("/api/chat/history", headers=self._authorized_headers(), name="/api/chat/history")

    @task(3)
    def chat_stream(self):
        if not self.enable_stream:
            return

        payload = {
            "subject": self.subject,
            "message": choice(self.messages),
        }

        with self.client.post(
            "/api/chat/stream",
            json=payload,
            headers=self._authorized_headers(),
            name="/api/chat/stream",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"chat stream failed: {response.status_code} {response.text}")
                return

            body = response.text
            if "event: done" not in body:
                response.failure("missing SSE done event")
                return
            if "event: chunk" not in body:
                response.failure("missing SSE chunk event")
                return
            response.success()
