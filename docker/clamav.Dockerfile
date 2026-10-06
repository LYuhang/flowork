# Keep ClamAV's entrypoint/signature database while applying OS security fixes.
FROM clamav/clamav:1.5.4-debian13-slim@sha256:9bb8712a50f0e75166e936c452cd82dd5e5be0b85586598930b5bbb84a99a578
RUN apt-get update && apt-get upgrade -y --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*
