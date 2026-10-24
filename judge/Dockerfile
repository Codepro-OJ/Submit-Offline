FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends g++ coreutils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /work
USER 65534:65534
