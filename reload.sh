#!/bin/bash
set -e

# Clean up unused images/containers to free disk space
docker system prune -f

docker compose pull
docker compose up -d
