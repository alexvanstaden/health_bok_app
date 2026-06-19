# The Next.js Web App (ADR-0009). Multi-stage: build, then run only the traced
# standalone output for a small image.
FROM node:22-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
COPY . .
# NEXT_PUBLIC_* is inlined into the client bundle at build time, not read at
# runtime — so the browser's API base URL must be fixed here, not in the runner's
# environment (ADR-0009). Compose passes the tailnet address through as a build arg.
ARG NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
RUN npm run build

FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
# Next's standalone output bundles the minimal server + node_modules it traced.
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
EXPOSE 3000
CMD ["node", "server.js"]
