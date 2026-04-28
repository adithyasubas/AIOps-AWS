FROM --platform=linux/amd64 node:18-alpine

WORKDIR /app

# Copy package files first
COPY package*.json ./

# Install dependencies
RUN npm ci --only=production

# Copy app code
COPY . .

# Expose port
EXPOSE 3000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
  CMD wget -qO- http://localhost:3000/health || exit 1

# Run as non-root
USER node

CMD ["node", "server.js"]
