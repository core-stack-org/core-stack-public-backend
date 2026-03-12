#!/bin/bash

set -e

# ==============================
# Configuration
# ==============================
ENV_NAME="corestack-env"
ENV_FILE="environment.yml"

# ==============================
# Functions
# ==============================

function check_conda() {
    if ! command -v conda &> /dev/null; then
        echo "❌ Conda not found."
        echo "Please install Miniforge or Conda before running this script."
        exit 1
    fi

    echo "✔ Conda found: $(conda info | grep 'base environment')"
}

function remove_existing_env() {
    if conda env list | grep -q "^$ENV_NAME "; then
        echo "⚠ Environment '$ENV_NAME' exists. Removing..."
        conda env remove -n "$ENV_NAME" -y
        echo "✔ Old environment removed."
    else
        echo "✔ No existing environment named '$ENV_NAME'."
    fi
}

function create_env() {
    if [ ! -f "$ENV_FILE" ]; then
        echo "❌ $ENV_FILE not found."
        exit 1
    fi

    echo "🚀 Creating environment from $ENV_FILE..."
    conda env create -f "$ENV_FILE" -n "$ENV_NAME"
    echo "✔ Environment created successfully."
}

# ==============================
# Main
# ==============================

echo "🔎 Checking conda..."
check_conda

echo "🧹 Cleaning old environment..."
remove_existing_env

echo "📦 Installing environment..."
create_env

echo ""
echo "================================="
echo "✅ Setup complete!"
echo "Activate using:"
echo "conda activate $ENV_NAME"
echo "================================="
