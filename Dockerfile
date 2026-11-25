# syntax=docker/dockerfile:1
FROM python:3.13.7-alpine

ARG UID=1000
ARG GID=1000

RUN addgroup -g $GID thirteener
RUN adduser -D -g '' -u $UID -G thirteener thirteener

# Generic labels
LABEL maintainer="Arian Mollik Wasi <arianmollik323@gmail.com>"
LABEL version="0.3.4"
LABEL description="My own custom 12ft.io replacement"
LABEL url="https://github.com/wasi-master/13ft/"
LABEL documentation="https://github.com/wasi-master/13ft/blob/main/README.md"

# OCI compliant labels
LABEL org.opencontainers.image.source="https://github.com/wasi-master/13ft"
LABEL org.opencontainers.image.authors="Arian Mollik Wasi"
LABEL org.opencontainers.image.created="2023-10-31T22:53:00Z"
LABEL org.opencontainers.image.version="0.3.4"
LABEL org.opencontainers.image.url="https://github.com/wasi-master/13ft/"
LABEL org.opencontainers.image.source="https://github.com/wasi-master/13ft/"
LABEL org.opencontainers.image.description="My own custom 12ft.io replacement"
LABEL org.opencontainers.image.documentation="https://github.com/wasi-master/13ft/blob/main/README.md"
LABEL org.opencontainers.image.licenses=MIT

RUN python -m pip install --root-user-action=ignore --upgrade pip

# Dependencies are installed as root, before the app is copied, so this layer
# is cached across code changes. lxml, regex, charset-normalizer and
# MarkupSafe publish musl wheels for amd64 and arm64; on 32-bit ARM (arm/v7)
# "regex" has none and is compiled, so a compiler is present for this step only.
COPY requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
  apk add --no-cache --virtual .build-deps gcc musl-dev \
  && pip install --root-user-action=ignore -r /tmp/requirements.txt \
  && apk del .build-deps

# The archive (SQLite) lives on a volume so it survives image updates.
RUN mkdir -p /data && chown thirteener:thirteener /data
VOLUME /data

COPY . .
USER thirteener

# Inside the container the server must listen on all interfaces for the port
# mapping to work; docker-compose.yaml publishes it on 127.0.0.1 only.
ENV HOST=0.0.0.0 \
    DATA_DIR=/data

WORKDIR /app
EXPOSE 5000
CMD [ "python", "-m", "gunicorn", "portable:app" ]
