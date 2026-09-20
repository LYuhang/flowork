# vibecanvas-engine — pure-Python package image
#
# Build:
#   docker build -t vibecanvas-engine:dev .
#
# This image is built by the supply-chain gate to verify that the standalone
# engine package remains installable. Product workflow execution is owned by
# the API-managed sandbox runtime, not a container CLI.

FROM python:3.11.16-slim-trixie@sha256:9c900dea9e8fb7e16277c179b555cc72d29a352dbc33cff48ad5a0412fd5bfc7

WORKDIR /app

# Install reviewed, hash-locked build tooling and runtime dependencies first.
COPY requirements-build.txt /app/requirements-build.txt
COPY requirements-runtime.txt /app/requirements-runtime.txt
RUN pip install --no-cache-dir --require-hashes -r requirements-build.txt && \
    pip install --no-cache-dir --require-hashes -r requirements-runtime.txt

# Copy the package metadata and source.
COPY pyproject.toml README.md /app/
COPY src/ /app/src/

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
