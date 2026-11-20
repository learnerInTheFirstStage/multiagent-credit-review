# Build stage: run Vite to produce the static bundle.
FROM node:22-alpine AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
# Auth0 sign-in, read by Vite at build time (src/auth/auth0.ts). Empty: the dev login page.
ARG VITE_AUTH0_DOMAIN=""
ARG VITE_AUTH0_CLIENT_ID=""
ARG VITE_AUTH0_AUDIENCE=""
ENV VITE_AUTH0_DOMAIN=$VITE_AUTH0_DOMAIN VITE_AUTH0_CLIENT_ID=$VITE_AUTH0_CLIENT_ID VITE_AUTH0_AUDIENCE=$VITE_AUTH0_AUDIENCE
RUN npm run build

# Serve stage: copy dist/ into nginx.
FROM nginx:1.27-alpine

# React Router does client-side routing. Nginx must serve index.html for
# every path it doesn't have a file for, otherwise deep links 404 on refresh.
COPY nginx.conf /etc/nginx/conf.d/default.conf

COPY --from=build /app/dist /usr/share/nginx/html
