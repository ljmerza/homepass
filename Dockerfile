# ── Stage 1: Builder ────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && TWARCH=$([ "$TARGETARCH" = "arm64" ] && echo "arm64" || echo "x64") \
    && curl -sL -o /usr/local/bin/tailwindcss \
       "https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.16/tailwindcss-linux-${TWARCH}" \
    && chmod +x /usr/local/bin/tailwindcss

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p static/icons && python generate_icons.py
RUN tailwindcss -i static/input.css -o static/dist.css --minify

# Swagger UI for /api/docs, self-hosted so the docs page runs under the app's
# normal CSP with no CDN allowance. Pinned by tag and by content hash: the tag
# says which release, the hashes make sure a moved tag or a tampered download
# fails the build instead of shipping. Fetched after Tailwind on purpose — the
# Tailwind content glob covers static/**/*.js, and a 1.5 MB bundle scanned for
# class names would only bloat dist.css. Bumping the version means updating
# all three hashes (sha256sum of each file at the new tag).
ARG SWAGGER_UI_VERSION=v5.32.15
RUN mkdir -p static/vendor/swagger-ui && cd static/vendor/swagger-ui \
    && base="https://raw.githubusercontent.com/swagger-api/swagger-ui/${SWAGGER_UI_VERSION}" \
    && curl -fsSL -o swagger-ui-bundle.js "${base}/dist/swagger-ui-bundle.js" \
    && curl -fsSL -o swagger-ui.css "${base}/dist/swagger-ui.css" \
    && curl -fsSL -o LICENSE "${base}/LICENSE" \
    && printf '%s  %s\n' \
       a7e344f2770b2f07527ce828e0951626983b8f2dcdb7a826689c0232023f995b swagger-ui-bundle.js \
       d7f39f764aa18c7b47dd05b9af5613e373e4ac0f3557c2693d52d0abc2464d76 swagger-ui.css \
       cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30 LICENSE \
       | sha256sum -c -

ARG GIT_SHA=dev
# Build timestamp too: local builds all get GIT_SHA=dev, and a byte-identical
# sw.js means browsers never install the new worker.
RUN sed -i "s/CACHE_VERSION_PLACEHOLDER/homepass-${GIT_SHA}-$(date +%s)/" static/sw.js

# ── Stage 2: Runtime ────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /build/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only runtime files (no Tailwind binary, no curl, no generate_icons.py)
COPY --from=builder /build/app ./app
COPY --from=builder /build/main.py .
COPY --from=builder /build/alembic.ini .
COPY --from=builder /build/migrations ./migrations
COPY --from=builder /build/templates ./templates
COPY --from=builder /build/static ./static
COPY --from=builder /build/run.sh .
RUN chmod +x run.sh

RUN mkdir -p /data

# Last of the cacheable instructions on purpose: GIT_SHA changes on every
# commit, and an ARG placed above the pip install would invalidate that layer on
# every build. app/build.py reads it to stamp ?v= on static asset URLs; the
# builder stage declares its own copy for the service-worker cache name, equally
# late, where nothing is left below it to invalidate.
ARG GIT_SHA=dev
ENV GIT_SHA=${GIT_SHA}

EXPOSE 5880

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=15s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://localhost:{os.environ.get(\"PORT\",5880)}/health')"

CMD ["/app/run.sh"]
