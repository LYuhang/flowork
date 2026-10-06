# Preserve PostgreSQL 17 and the official entrypoint/data-volume contract.
# Apply distribution security updates independently of upstream image rebuilds.
FROM postgres:17.11-trixie@sha256:d74eeac9a635390a49bc21bd49fccd973de707e2a53a76ac49b552b8712ec46f
RUN apt-get update && apt-get upgrade -y --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*
