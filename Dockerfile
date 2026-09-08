# One image, four commands. `serve` runs the web app, `worker` runs a
# discovery worker, and `queue-discovery` / `collect-discovery` are the two
# ends of the schedule. They differ only in argv, so a worker can never be
# running different lookup code from the collector that files its results.
FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY jobscout/ jobscout/

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir ".[api,service]"


FROM python:3.12-slim AS runtime

# Unprivileged, with a fixed uid so the manifests can pin runAsUser and the
# state volume can be owned by something specific rather than by root.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin jobscout

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    JOBSCOUT_DATA_DIR=/data \
    JOBSCOUT_HOST=0.0.0.0

# The data directory is a mount point. jobscout refuses to put personal data
# inside its own source tree, and in a container that check is the difference
# between state on a volume and state that vanishes with the pod.
RUN mkdir -p /data && chown 10001:10001 /data
VOLUME ["/data"]

WORKDIR /home/jobscout
USER 10001

ENTRYPOINT ["jobscout"]
CMD ["--help"]
