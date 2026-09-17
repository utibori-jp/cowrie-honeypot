# One image behind both the scheduled jobs and Jupyter, so the code explored
# in a notebook is the code the pipeline runs.
FROM python:3.13-slim

# uid 1000 / gid 100 matches the jovyan user the jupyter chart runs as, which
# is what owns the files on the shared Longhorn volume.
RUN useradd --uid 1000 --gid 100 --create-home --shell /bin/bash analyst

WORKDIR /app

# Dependencies resolve from the metadata alone, so they stay cached while the
# source underneath changes.
COPY pyproject.toml ./
COPY src/honeypot_analytics/__init__.py src/honeypot_analytics/__init__.py
RUN pip install --no-cache-dir '.[notebook]'

COPY src/ src/
RUN pip install --no-cache-dir --no-deps -e .

# Written by the chart. The default matches the mount path on the cluster.
ENV HONEYPOT_DATA_DIR=/data

USER 1000:100

ENTRYPOINT ["honeypot-analytics"]
