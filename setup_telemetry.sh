#!/bin/bash
# Setup script for Zerobus telemetry integration using uv

set -e

echo "🚀 Setting up Zerobus Telemetry Integration"
echo "==========================================="
echo ""

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    echo "📥 uv not found. Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    echo ""
    echo "✅ uv installed. Please restart your shell or run:"
    echo "   source \$HOME/.cargo/env"
    echo ""
    echo "Then run this script again."
    exit 0
fi

echo "✓ uv found: $(uv --version)"
echo ""

# Sync dependencies from pyproject.toml (includes Zerobus SDK wheel)
echo "📦 Syncing dependencies with uv..."
if [ -f "databricks_zerobus_ingest_sdk-0.1.0-py3-none-any.whl" ]; then
    uv sync
    uv pip install --force-reinstall databricks_zerobus_ingest_sdk-0.1.0-py3-none-any.whl
    echo "✅ All dependencies installed (including Zerobus SDK)"
else
    echo "⚠️  Warning: databricks_zerobus_ingest_sdk-0.1.0-py3-none-any.whl not found"
    echo "   Place the wheel file in the project root before running setup"
    echo "   Continuing with other dependencies..."
    uv sync --no-install-project
fi

echo ""
echo "✅ Dependencies installed!"
echo ""

# Generate protobuf schema from Unity Catalog table
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📋 Generating Protobuf Schema from UC Table"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""



echo "🔨 Generating protobuf schema from UC table: $TABLE"
uv run python -m zerobus.tools.generate_proto \
    --uc-endpoint "$UC_ENDPOINT" \
    --table "$TABLE" \
    --client-id "$CLIENT_ID" \
    --client-secret "$CLIENT_SECRET" \
    --proto-msg "$MESSAGE_NAME" \
    --output "$OUTPUT"

if [ $? -eq 0 ]; then
    echo "✅ Protobuf schema generated: $OUTPUT"
    
    # Compile the generated proto file
    echo "🔨 Compiling protobuf schema..."
    uv run python -m grpc_tools.protoc \
        --python_out=. \
        --proto_path=. \
        "$OUTPUT"
    
    if [ $? -eq 0 ]; then
        echo "✅ Protobuf compiled successfully! (record_pb2.py created)"
    else
        echo "❌ Failed to compile protobuf schema"
        exit 1
    fi
else
    echo "❌ Failed to generate protobuf schema"
    exit 1
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ Setup complete!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Next steps:"
echo "1. Configure environment variables:"
echo "   cp env.example .env && vim .env"
echo "2. Create a Unity Catalog table for telemetry"
echo "3. Run the app:"
echo "   uv run python app_dash.py"
echo ""
echo "📖 Documentation: README.md"
