FROM node:22-bookworm
WORKDIR /node
COPY package.json package-lock.json ./
# hsd's database requires its native bindings, even for a regtest node.
RUN npm ci --omit=dev
ENV NODE_BACKEND=js
CMD ["node", "node_modules/hsd/bin/hsd", "--network=regtest", "--prefix=/data", "--index-tx=true", "--index-address=true", "--http-host=0.0.0.0", "--wallet-http-host=0.0.0.0", "--listen=false", "--workers=false", "--log-level=warning"]
