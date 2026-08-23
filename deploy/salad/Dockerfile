# syntax=docker/dockerfile:1.7

FROM python:3.11-slim@sha256:9c900dea9e8fb7e16277c179b555cc72d29a352dbc33cff48ad5a0412fd5bfc7 AS model-cache

COPY deploy/salad/download_model.py /tmp/download_model.py
RUN python -m pip install --no-cache-dir "huggingface-hub==1.28.0" \
    && HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 python /tmp/download_model.py \
    && python -m pip uninstall --yes huggingface-hub hf-xet \
    && rm -rf /root/.cache /tmp/download_model.py

FROM ghcr.io/evrenesat/audioventura-ace-step-worker@sha256:0310fef73053113f0060bc4861e7b682156f375159386473356a4dc4c9850846

ARG SALAD_QUEUE_WORKER_VERSION=0.7.0
ARG SALAD_QUEUE_WORKER_SHA256=074a329cf6462e77fc7b72100f59d8a690831456d9420186a834a8f30634c9e4

LABEL org.opencontainers.image.title="AudioVentura ACE-Step Salad worker" \
      org.opencontainers.image.source="https://github.com/evrenesat/audioventura" \
      io.audioventura.model.repo="evrenesat/audioventura-ace-step-v0.1.8" \
      io.audioventura.model.revision="88b8c7fa089446b53382c1040037492463430bed" \
      io.audioventura.model.bundle="av-v0.1.8-bundle-2"

ENV ACE_WORKER_MODEL_REPO=evrenesat/audioventura-ace-step-v0.1.8 \
    ACE_WORKER_MODEL_REVISION=88b8c7fa089446b53382c1040037492463430bed \
    ACE_WORKER_MODEL_TAG=av-v0.1.8-bundle-2 \
    ACE_WORKER_MODEL_MANIFEST_SHA256=39a8180ef6852e2dfccb9088efa7231ca7de7e4c05c8d65e3ac5a3e7a5bfd0fc \
    ACE_WORKER_HF_CACHE_ROOT=/opt/audioventura-model/huggingface-cache/hub \
    ACE_TRANSFER_ALLOWED_HOST=player.evren.io \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    SALAD_LOG_LEVEL=info

RUN apt-get update \
    && apt-get install --yes --no-install-recommends curl \
    && curl --fail --location --output /tmp/salad-worker.tar.gz \
        "https://github.com/SaladTechnologies/salad-cloud-job-queue-worker/releases/download/v${SALAD_QUEUE_WORKER_VERSION}/salad-http-job-queue-worker_x86_64.tar.gz" \
    && echo "${SALAD_QUEUE_WORKER_SHA256}  /tmp/salad-worker.tar.gz" | sha256sum --check --strict \
    && tar --extract --gzip --file /tmp/salad-worker.tar.gz --directory /usr/local/bin \
    && test -x /usr/local/bin/salad-http-job-queue-worker \
    && rm -rf /var/lib/apt/lists/* /tmp/salad-worker.tar.gz

COPY --link --from=model-cache /opt/audioventura-model /opt/audioventura-model
COPY --link runpod_worker /opt/runpod_worker
COPY --link deploy/salad/__init__.py deploy/salad/worker_api.py /opt/salad/
COPY --link --chmod=755 deploy/salad/entrypoint.sh /opt/salad/entrypoint.sh

WORKDIR /opt
EXPOSE 8080
ENTRYPOINT ["/opt/salad/entrypoint.sh"]
