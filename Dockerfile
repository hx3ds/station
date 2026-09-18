FROM python:3.12-slim-bookworm AS builder
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /workspace
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential cargo gcc g++ libffi-dev libolm-dev libssl-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml requirements.txt /workspace/station/
COPY src /workspace/station/src
COPY README.md /workspace/station/README.md
RUN pip install --user /workspace/station

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /workspace
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH
ENTRYPOINT ["python3", "-m", "station.run_station"]
