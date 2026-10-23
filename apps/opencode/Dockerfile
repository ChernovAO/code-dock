FROM ghcr.io/anomalyco/opencode:2.1.2

ARG HOST_UID=1000
ARG HOST_GID=1000

RUN apk add --no-cache git bash \
    && addgroup -g ${HOST_GID} app \
    && adduser -D -u ${HOST_UID} -G app -h /opencode app \
    && install -d -m 755 /etc/opencode

# Каталог создан выше: COPY --chmod выставляет режим 644 и созданному им каталогу,
# app не может в него зайти, и opencode молча работает без системного конфига.
COPY --chmod=644 opencode.jsonc /etc/opencode/opencode.jsonc

# Конфиг репозитория (formatter, lsp, провайдеры, агенты) обходит права и исполняет код, поэтому не грузим его.
# Побочный эффект: AGENTS.md проекта не подхватывается, инструкции будет передавать core.
ENV HOME=/opencode \
    OPENCODE_DISABLE_PROJECT_CONFIG=1

USER app
