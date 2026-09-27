FROM python:3.13-slim

ARG VERSION=dev
LABEL org.opencontainers.image.title="flow-vs-snmp" \
      org.opencontainers.image.description="Compare Kentik flow-derived bit rates against SNMP for one router interface" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

# Unprivileged runtime user. /srv/outputs is where the "Save files" option writes; mount a
# host directory there to keep those captures (see README).
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /srv/outputs \
    && chown app:app /srv/outputs

COPY .streamlit/ .streamlit/
COPY app/ app/

USER app

EXPOSE 8501

# python rather than curl — the slim image doesn't ship curl.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4)"

CMD ["streamlit", "run", "app/app.py", "--server.address=0.0.0.0", "--server.port=8501"]
