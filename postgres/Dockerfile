# Keep the existing PG 15 / bookworm volume and locale contract, while taking
# OS security updates independently of pgvector's infrequently rebuilt image.
# Entrypoint behavior follows docker-library/postgres at the pinned commit;
# its MIT license is retained in the image. Only the privilege drop changes
# from a separately bundled Go helper to Debian's maintained setpriv utility.
FROM debian:bookworm-20260918-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251 AS postgres-runtime
ENV PG_MAJOR=15 PG_VERSION=15.19-1.pgdg12+2 \
    PGDATA=/var/lib/postgresql/data LANG=en_US.utf8 \
    PATH=/usr/lib/postgresql/15/bin:$PATH
ARG POSTGRES_SCRIPTS_COMMIT=3cfb3b50d63453da51a439b25a1e18af563c3e2b
RUN set -eux; \
    groupadd -r postgres --gid=999; \
    useradd -r -g postgres --uid=999 --home-dir=/var/lib/postgresql --shell=/bin/bash postgres; \
    if [ -f /etc/dpkg/dpkg.cfg.d/docker ]; then \
        sed -ri '/\/usr\/share\/locale/d' /etc/dpkg/dpkg.cfg.d/docker; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl locales libnss-wrapper xz-utils zstd util-linux; \
    echo 'en_US.UTF-8 UTF-8' >> /etc/locale.gen; \
    locale-gen; \
    locale -a | grep -F en_US.utf8; \
    mkdir -p /usr/local/share/keyrings /usr/share/doc/docker-postgres /docker-entrypoint-initdb.d; \
    curl --proto '=https' --tlsv1.2 -fsSL --retry 3 \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/local/share/keyrings/postgres.asc; \
    echo '0144068502a1eddd2a0280ede10ef607d1ec592ce819940991203941564e8e76  /usr/local/share/keyrings/postgres.asc' | sha256sum -c -; \
    echo 'deb [signed-by=/usr/local/share/keyrings/postgres.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main 15' > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends postgresql-common; \
    sed -ri 's/#?(create_main_cluster) .*$/\1 = false/' /etc/postgresql-common/createcluster.conf; \
    apt-get install -y --no-install-recommends "postgresql-${PG_MAJOR}=${PG_VERSION}"; \
    postgres --version | grep -F '15.19'; \
    for script in docker-entrypoint.sh docker-ensure-initdb.sh; do \
        curl --proto '=https' --tlsv1.2 -fsSL --retry 3 \
            "https://raw.githubusercontent.com/docker-library/postgres/${POSTGRES_SCRIPTS_COMMIT}/${script}" \
            -o "/usr/local/bin/${script}"; \
    done; \
    curl --proto '=https' --tlsv1.2 -fsSL --retry 3 \
        "https://raw.githubusercontent.com/docker-library/postgres/${POSTGRES_SCRIPTS_COMMIT}/LICENSE" \
        -o /usr/share/doc/docker-postgres/LICENSE; \
    echo '9c440299ae04a0a79d55b8bf03307036d890a40979d2fb698073c9050d4b20a5  /usr/local/bin/docker-entrypoint.sh' | sha256sum -c -; \
    echo '922ade6b23a312e65023349b36e873752b15ff38ae4c0ae461d74451e104f312  /usr/local/bin/docker-ensure-initdb.sh' | sha256sum -c -; \
    echo '87ffd2c45e3f90cfa3407b5c40ef8333e87c3e875e4895f8b64df758198deafc  /usr/share/doc/docker-postgres/LICENSE' | sha256sum -c -; \
    for script in /usr/local/bin/docker-entrypoint.sh /usr/local/bin/docker-ensure-initdb.sh; do \
        test "$(grep -c 'exec gosu postgres' "$script")" = 1; \
        sed -i 's/exec gosu postgres /exec setpriv --reuid=postgres --regid=postgres --init-groups /' "$script"; \
        chmod 0755 "$script"; \
    done; \
    ln -s docker-ensure-initdb.sh /usr/local/bin/docker-enforce-initdb.sh; \
    sed -ri "s!^#?(listen_addresses)\s*=\s*\S+.*!\1 = '*'!" "/usr/share/postgresql/${PG_MAJOR}/postgresql.conf.sample"; \
    install -d -o postgres -g postgres -m 1777 /var/lib/postgresql "$PGDATA"; \
    install -d -o postgres -g postgres -m 3777 /var/run/postgresql; \
    rm -rf /var/lib/apt/lists/*

FROM postgres-runtime AS vector-build
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends build-essential "postgresql-server-dev-${PG_MAJOR}=${PG_VERSION}"; \
    curl --proto '=https' --tlsv1.2 -fsSL --retry 3 \
        https://codeload.github.com/pgvector/pgvector/tar.gz/8ee86c96f0fd72390f890aa8a336fda6d3ab4c6c \
        -o /tmp/pgvector.tar.gz; \
    echo 'd076a3098010905fd60256649327809651f6288327db6413f0938305f62ea299  /tmp/pgvector.tar.gz' | sha256sum -c -; \
    mkdir /build; \
    tar -xzf /tmp/pgvector.tar.gz -C /build --strip-components=1; \
    make -C /build OPTFLAGS=""; \
    make -C /build install DESTDIR=/out; \
    install -D -m 0644 /build/LICENSE /out/usr/share/doc/pgvector/LICENSE; \
    test -f /out/usr/lib/postgresql/15/lib/vector.so

FROM postgres-runtime
COPY --from=vector-build /out/ /
VOLUME /var/lib/postgresql/data
ENTRYPOINT ["docker-entrypoint.sh"]
STOPSIGNAL SIGINT
EXPOSE 5432
CMD ["postgres"]
