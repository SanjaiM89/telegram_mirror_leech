#!/bin/bash

set -e  # Exit immediately if any command fails

IMAGE_NAME="telegram_mirror_leech"
CONTAINER_NAME="telegram_mirror_leech"
NETWORK_NAME="wzml-net"

echo "📥 Pulling latest code..."
git pull origin main

echo "🐳 Building Docker image..."
sudo docker build . -t $IMAGE_NAME

echo "🛑 Stopping existing container (if running)..."
sudo docker stop $CONTAINER_NAME 2>/dev/null || true

echo "🧹 Removing existing container (if exists)..."
sudo docker rm $CONTAINER_NAME 2>/dev/null || true

echo "🚀 Running new container..."
sudo docker run -d \
  --name $CONTAINER_NAME \
  --network $NETWORK_NAME \
  -p 7474:80 \
  -p 7373:8080 \
  $IMAGE_NAME

echo "✅ Deployment completed successfully"

