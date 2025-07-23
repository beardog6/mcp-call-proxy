#!/bin/bash
docker stop mcp-call-proxy
docker rm mcp-call-proxy
docker build . -t mcp-call-proxy:$(date +%Y%m%d)
