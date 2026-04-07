FROM python:3.12-slim

WORKDIR /app
ENV MINERU_VENV=/tmp/mineru-venv

RUN apt-get update \
    && apt-get install -y --no-install-recommends libxcb1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && python -m venv "${MINERU_VENV}" \
    && "${MINERU_VENV}/bin/pip" install --no-cache-dir --upgrade pip setuptools wheel \
    && "${MINERU_VENV}/bin/pip" install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple "mineru[core]"

COPY . /app

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
