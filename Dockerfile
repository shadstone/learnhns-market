FROM node:20-alpine AS frontend

WORKDIR /build

COPY package.json package-lock.json tailwind.config.js ./
RUN npm ci --ignore-scripts

COPY app/templates ./app/templates
COPY app/static/css/tailwind-input.css ./app/static/css/tailwind-input.css
RUN npm run build:css

FROM node:22-bookworm-slim AS verifier-runtime
WORKDIR /verifier
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts

FROM python:3.12-slim-bookworm

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=verifier-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=verifier-runtime /verifier/node_modules ./node_modules

COPY . .
COPY --from=frontend /build/app/static/css/tailwind.css ./app/static/css/tailwind.css

RUN chmod +x start.sh

EXPOSE 8000

CMD ["./start.sh"]
