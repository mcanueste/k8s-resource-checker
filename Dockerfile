FROM python:3.8-slim-bullseye
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y -q --no-install-recommends \
    wget \
    ssh \
    git \
    git-lfs \
    build-essential \
    ca-certificates && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
