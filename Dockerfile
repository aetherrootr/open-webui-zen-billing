# The functions use Open WebUI internal APIs. Match this base to the deployed version.
ARG OPEN_WEBUI_IMAGE=ghcr.io/open-webui/open-webui:v0.12.5
FROM ${OPEN_WEBUI_IMAGE} AS base
ENV PYTHONPATH=/app/backend \
    PYTHONUNBUFFERED=1 \
    BILLING_PLUGIN_DIR=/opt/open-webui-zen-billing/plugins
COPY plugins/ /opt/open-webui-zen-billing/plugins/
COPY installer/ /opt/open-webui-zen-billing/installer/
COPY VERSION /opt/open-webui-zen-billing/VERSION
WORKDIR /app/backend

FROM base AS test
COPY tests/ /opt/open-webui-zen-billing/tests/
RUN PYTHONPATH=/opt/open-webui-zen-billing/plugins:/opt/open-webui-zen-billing/installer:/app/backend \
    python -m unittest discover -s /opt/open-webui-zen-billing/tests -v

FROM base AS runtime
LABEL org.opencontainers.image.title="Open WebUI Zen Billing" \
      org.opencontainers.image.version="0.1.0" \
      io.aetherrootr.open-webui.version="0.12.5"
ENTRYPOINT ["python", "/opt/open-webui-zen-billing/installer/install.py"]
CMD []
