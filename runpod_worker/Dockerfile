# ACE-Step v0.1.8 Runpod Serverless Flex worker.
# Model weights are external to this image and must be present in the endpoint
# cached-model location or network volume before a worker starts.

FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04

ARG DEBIAN_FRONTEND=noninteractive
ARG ACE_STEP_REPOSITORY=https://github.com/ace-step/ACE-Step-1.5.git
ARG ACE_STEP_TAG=v0.1.8

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TOKENIZERS_PARALLELISM=false \
    ACE_WORKER_CHECKPOINTS_DIR=/runpod-volume/checkpoints \
    ACE_TRANSFER_ALLOWED_HOST= \
    ACESTEP_DISABLE_TQDM=true

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential \
        ca-certificates \
        git \
        libsndfile1 \
        software-properties-common \
    && add-apt-repository --yes ppa:deadsnakes/ppa \
    && apt-get update \
    && apt-get install --yes --no-install-recommends \
        python3.11 \
        python3.11-dev \
        python3.11-venv \
    && rm -rf /var/lib/apt/lists/*

RUN python3.11 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

RUN git clone --quiet --depth 1 --branch "${ACE_STEP_TAG}" "${ACE_STEP_REPOSITORY}" /opt/ace-step \
    && python -m pip install --upgrade pip \
    && python -m pip install --extra-index-url https://download.pytorch.org/whl/cu128 \
        /opt/ace-step/acestep/third_parts/nano-vllm \
        /opt/ace-step \
    && python -m pip install "lameenc==1.8.4" "runpod==1.7.9"

WORKDIR /opt
COPY runpod_worker /opt/runpod_worker

RUN mkdir -p /runpod-volume/checkpoints \
    && chmod 700 /runpod-volume/checkpoints

CMD ["python", "-m", "runpod_worker.handler"]
