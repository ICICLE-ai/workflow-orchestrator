# Harvest Workflow Orchestrator — frontend (React Router v7 + Mantine)
#
# Build from the frontend/ directory:
#   DOCKER_BUILDKIT=1 docker build -t harvest-frontend \
#     --secret id=npmrc,src=$HOME/.npmrc \
#     --build-arg VITE_BACKEND_URL=https://api.example.org frontend/
#
# The --secret is REQUIRED. The @icicle-ai/* UI packages are published to GitHub
# Packages, not the public npm registry, so `npm ci` needs both the scope->
# registry mapping and a token — and those live in the developer's ~/.npmrc, not
# in the repo. It's mounted as a BuildKit secret rather than COPYed so the token
# is readable during that RUN only and never lands in an image layer or in
# `docker history`. In CI, write a temporary .npmrc from a repo secret and point
# src= at it.
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
RUN --mount=type=secret,id=npmrc,target=/root/.npmrc npm ci

FROM node:22-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Baked into the client bundle — see the note above. The defaults match
# app/lib/api.ts's own fallbacks, so a build with no args still produces a
# working local-dev image rather than a blank string.
ARG VITE_BACKEND_URL=http://localhost:8002
ARG VITE_SAM3_ENDPOINT=
ENV VITE_BACKEND_URL=$VITE_BACKEND_URL \
    VITE_SAM3_ENDPOINT=$VITE_SAM3_ENDPOINT

RUN npm run build

# Production dependencies only, resolved in their own stage so the dev
# toolchain (vite, typescript, tailwind) never reaches the runtime image.
FROM node:22-alpine AS prod-deps
WORKDIR /app
COPY package.json package-lock.json .npmrc ./
RUN --mount=type=secret,id=npmrc,target=/root/.npmrc \
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
