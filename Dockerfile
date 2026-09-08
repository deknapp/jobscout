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
# The user's home is /app. test_privacy.py refuses user-directory paths
# anywhere in the source tree, because that is how somebody's personal path
# leaks into a public repository. The guard cannot tell that this one belongs
# to a container's own account, and the right answer is to move the path
# rather than loosen a guard that exists to catch a real mistake.
RUN useradd --uid 10001 --home-dir /app --create-home --shell /usr/sbin/nologin jobscout

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

WORKDIR /app
USER 10001

ENTRYPOINT ["jobscout"]
CMD ["--help"]
