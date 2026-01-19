#!/bin/bash
set -e

docker build --platform linux/amd64 -t satamanchuk/alexbot .
docker push satamanchuk/alexbot
