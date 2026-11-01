# ---------- Stage 1: build the Vite bundle ----------
FROM node:20-alpine AS build

WORKDIR /app

# Dependencies layer stays cached while source changes.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Vite inlines import.meta.env.VITE_* at BUILD time, so this must be a build
# arg, not a runtime env. Default is same-origin (''): nginx proxies /api and
# /v1 to the backend, and Caddy terminates HTTPS in front. This avoids shipping
# a bundle that calls http://localhost:8000 from a remote browser.
ARG VITE_API_BASE_URL=
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL

RUN npm run build

# ---------- Stage 2: serve the static SPA ----------
FROM nginx:alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
