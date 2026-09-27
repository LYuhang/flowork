# vibecanvas-engine — pure-Python package image
#
# Build:
#   docker build -f engine/Dockerfile -t vibecanvas-engine:dev .
#
# This image is built by the supply-chain gate to verify that the standalone
# engine package remains installable. Product workflow execution is owned by
# the API-managed sandbox runtime, not a container CLI.

FROM python:3.11.16-slim-trixie@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e

WORKDIR /app

COPY scripts/security/patch_python_tarfile.py /tmp/patch_python_tarfile.py
COPY scripts/security/verify_python_tarfile_fix.py /usr/local/lib/flowork/verify_python_tarfile_fix.py
RUN python /tmp/patch_python_tarfile.py && \
    python /usr/local/lib/flowork/verify_python_tarfile_fix.py && \
    rm /tmp/patch_python_tarfile.py

# Install reviewed, hash-locked build tooling and runtime dependencies first.
COPY engine/requirements-build.txt /app/requirements-build.txt
COPY engine/requirements-runtime.txt /app/requirements-runtime.txt
RUN pip install --no-cache-dir --require-hashes -r requirements-build.txt && \
    pip install --no-cache-dir --require-hashes -r requirements-runtime.txt

# Copy the package metadata and source.
COPY engine/pyproject.toml engine/README.md /app/
COPY engine/src/ /app/src/

# Install the package itself (resolves jsonschema, json_repair, jinja2, requests).
# --no-cache-dir keeps the image lean.
RUN pip install --no-cache-dir --no-deps --no-build-isolation .

# The runtime does not install packages. Drop installer tooling after the
# application is materialized so vulnerabilities in those tools are not
# carried into the production image.
RUN python -m pip uninstall -y \
      pip setuptools wheel hatchling editables packaging pathspec pluggy \
      trove-classifiers \
      jaraco.context && \
    test ! -e /usr/local/bin/pip && \
    test ! -e /usr/local/bin/pip3

# A direct container run performs a dependency/import smoke check. Applications
# consume the package through its Python API or through the Flowork platform.
CMD ["python", "-c", "import vibecanvas_engine; print('vibecanvas_engine ready')"]
