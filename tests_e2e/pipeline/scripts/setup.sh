#!/usr/bin/env bash

set -euox pipefail

# Add delay per Azure Pipelines documentation
sleep 30

sudo tdnf update -y

# Install Azure CLI
sudo tdnf install -y ca-certificates azure-cli


# Install Docker Engine
sudo tdnf install -y moby-containerd moby-cli moby-runc moby-engine

# Enable and Start Docker service
sudo systemctl enable docker
sudo systemctl start docker

# Verify that Docker Engine is installed correctly by running the hello-world image.
sudo docker run hello-world

# Install jq; it is used by the cleanup pipeline to parse the JSON output of the Azure CLI
sudo tdnf install -y jq

# Install Git
sudo tdnf install -y git