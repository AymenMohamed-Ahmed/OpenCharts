# syntax=docker/dockerfile:1
# Builds the OpenCharts terminal and serves it with nginx, which also proxies
# /api to the gateway. Build context is the repository root.

FROM node:20-slim AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# VITE_* values are compiled into the public bundle. Never pass secrets here.
ARG VITE_ADAPTER=demo
ARG VITE_HL_NETWORK=testnet
ENV VITE_ADAPTER=$VITE_ADAPTER VITE_HL_NETWORK=$VITE_HL_NETWORK
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime
COPY infra/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/nginx-health"]
