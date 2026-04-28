const express = require('express');
const os = require('os');

const app = express();
const PORT = process.env.PORT || 3000;

// Middleware: log all requests
app.use((req, res, next) => {
  console.log(`[${new Date().toISOString()}] ${req.method} ${req.url}`);
  next();
});

// Root endpoint
app.get('/', (req, res) => {
  res.json({
    message: "Chaos CICD Demo API",
    version: "1.0.0",
    region: process.env.AWS_REGION || "ap-southeast-2"
  });
});

// Health endpoint
app.get('/health', (req, res) => {
  res.json({
    status: "healthy",
    timestamp: new Date().toISOString(),
    uptime: process.uptime()
  });
});

// Info endpoint
app.get('/info', (req, res) => {
  res.json({
    hostname: os.hostname(),
    platform: os.platform(),
    nodeVersion: process.version
  });
});

// Graceful shutdown
process.on('SIGTERM', () => {
  console.log('SIGTERM received, shutting down...');
  process.exit(0);
});

app.listen(PORT, () => {
  console.log(`Server running on port ${PORT}`);
});