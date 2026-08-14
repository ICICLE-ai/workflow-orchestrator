# Harvest Workflow Orchestrator — frontend (React Router v7 + Mantine)
#
# Build from the frontend/ directory:
#   DOCKER_BUILDKIT=1 docker build -t harvest-frontend \
#     --secret id=npmrc,src=$HOME/.npmrc \
#     --build-arg VITE_BACKEND_URL=https://api.example.org frontend/
#
# The --secret is REQUIRED, and not because the @icicle-ai/* packages are
# private. GitHub Packages' npm registry rejects ANONYMOUS reads with 401
# regardless of package visibility — unlike ghcr.io, which serves public
# container images without credentials. Verified: an unauthenticated GET of
# @icicle-ai/tapis-file-explorer returns 401, and none of these packages exist
# on registry.npmjs.org (404), so there is no unauthenticated fallback.
# package-lock.json also pins each `resolved` URL to npm.pkg.github.com, so
# `npm ci` goes there whatever the registry config says.
#
# So the token is an IDENTITY check, not an authorization one: a PAT with
# `read:packages` and nothing else is sufficient, and GitHub Actions' built-in
# GITHUB_TOKEN works for org packages. It's still a user credential, hence the
# secret mount rather than a COPY: it's readable during that RUN only, and never
# lands in an image layer or in `docker history`. In CI, write a temporary
# .npmrc from a repo secret and point src= at it.
#
# Run:
#   docker run --rm -p 3000:3000 harvest-frontend
#
# VITE_BACKEND_URL IS A BUILD ARGUMENT, NOT A RUNTIME VARIABLE. Vite inlines
# every import.meta.env.* reference into the client bundle at build time (see
# app/lib/api.ts), so passing it with `docker run -e` has no effect whatsoever —
# the value is already compiled in. Pointing the app at a different backend
# means rebuilding the image. Same for VITE_SAM3_ENDPOINT.

FROM node:22-alpine AS deps
# Node 22, not the 20 the React Router starter template ships with: Vite 8
# requires ^20.19 || >=22.12, and 22 is what this project develops against.
WORKDIR /app
# Only the manifests, so `npm ci` is cached until dependencies actually change.
# .npmrc is REQUIRED, not incidental: it carries legacy-peer-deps=true, without
# which npm ci fails on ERESOLVE — the @icicle-ai/* UI packages declare react
# ^18 peers while this app runs react 19 (see the comment in .npmrc itself).
COPY package.json package-lock.json .npmrc ./
# `required=true` makes BuildKit fail immediately, and by name, when --secret is
# missing from the build command. Without it BuildKit mounts an EMPTY file, npm
# runs unauthenticated, and the build spends a minute downloading public
# packages before dying on a 401 that says "authentication token not provided"
# — which reads like a bad token rather than a missing build flag.
RUN --mount=type=secret,id=npmrc,target=/root/.npmrc,required=true \
    sh -c 'grep -q "npm.pkg.github.com/:_authToken" /root/.npmrc \
      || { echo "ERROR: the mounted .npmrc has no npm.pkg.github.com token." >&2; \
           echo "  The @icicle-ai/* packages come from GitHub Packages. Your ~/.npmrc needs:" >&2; \
           echo "    @icicle-ai:registry=https://npm.pkg.github.com/" >&2; \
           echo "    //npm.pkg.github.com/:_authToken=<a PAT with read:packages>" >&2; \
           exit 1; }; \
    npm ci'

FROM node:22-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Baked into the client bundle — see the note above. The defaults match the
# code's own fallbacks, so a build with no args still produces a working
# local-dev image rather than blank strings.
#
# EVERY import.meta.env.VITE_* the app reads must be listed here. A var that is
# only in frontend/.env and not an ARG is not "inherited" by a Docker build —
# .env isn't copied into the image, so the code silently falls back to its
# hardcoded default and the mismatch shows up as the app talking to the wrong
# Tapis tenant rather than as a build error.
ARG VITE_BACKEND_URL=http://localhost:8002
ARG VITE_SAM3_ENDPOINT=
ARG VITE_TAPIS_BASE_URL=https://icicleai.tapis.io
ARG VITE_DEFAULT_TAPIS_SYSTEM=expanse-tapis-static
ENV VITE_BACKEND_URL=$VITE_BACKEND_URL \
    VITE_SAM3_ENDPOINT=$VITE_SAM3_ENDPOINT \
    VITE_TAPIS_BASE_URL=$VITE_TAPIS_BASE_URL \
    VITE_DEFAULT_TAPIS_SYSTEM=$VITE_DEFAULT_TAPIS_SYSTEM

RUN npm run build

# Production dependencies only, resolved in their own stage so the dev
# toolchain (vite, typescript, tailwind) never reaches the runtime image.
FROM node:22-alpine AS prod-deps
WORKDIR /app
COPY package.json package-lock.json .npmrc ./
RUN --mount=type=secret,id=npmrc,target=/root/.npmrc,required=true \
    npm ci --omit=dev && npm cache clean --force

FROM node:22-alpine AS runtime
ENV NODE_ENV=production
WORKDIR /app

COPY package.json package-lock.json ./
COPY --from=prod-deps /app/node_modules ./node_modules
COPY --from=build /app/build ./build

# The node image ships an unprivileged `node` user; the server only reads from
# /app, so nothing needs to be writable.
USER node

# react-router-serve's default port. It honours PORT, so this one IS a genuine
# runtime variable, unlike the VITE_* args above.
EXPOSE 3000
ENV PORT=3000

CMD ["npm", "run", "start"]
