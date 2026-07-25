FROM node:20-alpine AS frontend

WORKDIR /build

COPY package.json package-lock.json tailwind.config.js ./
RUN npm ci

COPY app/templates ./app/templates
COPY app/static/css/tailwind-input.css ./app/static/css/tailwind-input.css
RUN npm run build:css

FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=frontend /build/app/static/css/tailwind.css ./app/static/css/tailwind.css

RUN chmod +x start.sh

EXPOSE 8000

CMD ["./start.sh"]
