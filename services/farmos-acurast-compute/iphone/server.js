#!/usr/bin/env node
/**
 * FARMOS Acurast Compute Service - iPhone
 * 
 * Simple serverless HTTP service for Acurast deployment on iPhone.
 * Provides /health and /compute endpoints.
 */

const http = require('http');

const PORT = process.env.PORT || 3000;
const processorId = process.env.PROCESSOR_ID || 'farmos-iphone';

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  const path = url.pathname;
  const method = req.method;

  res.setHeader('Content-Type', 'application/json');
  res.setHeader('Access-Control-Allow-Origin', '*');

  if (method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }

  if (path === '/health' && method === 'GET') {
    res.writeHead(200);
    res.end(JSON.stringify({
      status: 'ok',
      service: 'farmos-iphone-compute-service',
      timestamp: new Date().toISOString(),
      uptime: process.uptime(),
      processorId
    }));
  } else if (path === '/compute' && method === 'POST') {
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => {
      let input = {};
      try {
        if (body) input = JSON.parse(body);
      } catch (e) {
        res.writeHead(400);
        res.end(JSON.stringify({ error: 'Invalid JSON' }));
        return;
      }
      
      res.writeHead(200);
      res.end(JSON.stringify({
        received: input,
        timestamp: new Date().toISOString(),
        uptime: process.uptime(),
        processorId,
        compute: {
          echo: input.message || 'hello',
          length: (input.message || 'hello').length,
          reversed: (input.message || 'hello').split('').reverse().join(''),
          hash: simpleHash((input.message || 'hello') + Date.now().toString())
        }
      }));
    });
  } else {
    res.writeHead(404);
    res.end(JSON.stringify({ error: 'Not Found', path }));
  }
});

function simpleHash(str) {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    const char = str.charCodeAt(i);
    hash = ((hash << 5) - hash) + char;
    hash = hash & hash;
  }
  return Math.abs(hash).toString(16);
}

server.listen(PORT, '0.0.0.0', () => {
  console.log(`FARMOS iPhone Acurast Compute Service running on port ${PORT}`);
});

process.on('SIGTERM', () => {
  server.close(() => process.exit(0));
});
