#!/bin/bash
# LinkedIn MCP Server - AXI-Compliant CLI Install Script
# This script installs the linkedin-lyr with Obscura backend and AXI compliance
# Usage: curl -sSL https://raw.githubusercontent.com/ishan-parihar/linkedin-lyr/main/install.sh | bash

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Functions
info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if running as root
if [ "$EUID" -eq 0 ]; then
    error "Please do not run this script as root. Use a regular user account."
    exit 1
fi

# Detect OS
OS="$(uname -s)"
case "$OS" in
    Linux*)     MACHINE=Linux;;
    Darwin*)    MACHINE=Mac;;
    CYGWIN*)    MACHINE=Cygwin;;
    MINGW*)     MACHINE=MinGw;;
    *)          MACHINE="UNKNOWN:$OS"
esac

info "Detected OS: $MACHINE"

# Check Python version
info "Checking Python version..."
if ! command -v python3 &> /dev/null; then
    error "Python 3 is not installed. Please install Python 3.12 or higher."
    exit 1
fi

PYTHON_VERSION=$(python3 --version | awk '{print $2}')
PYTHON_MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
PYTHON_MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 11 ]); then
    error "Python 3.11 or higher is required. Found: $PYTHON_VERSION"
    exit 1
fi

success "Python version: $PYTHON_VERSION"

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    info "uv is not installed. Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    success "uv installed successfully"
else
    success "uv is already installed: $(uv --version)"
fi

# Set installation directory
INSTALL_DIR="${HOME}/.linkedin-lyr"
REPO_DIR="${INSTALL_DIR}/linkedin-lyr"

info "Installation directory: $INSTALL_DIR"

# Create installation directory
mkdir -p "$INSTALL_DIR"

# Clone or update repository
if [ -d "$REPO_DIR" ]; then
    info "Repository already exists. Updating..."
    cd "$REPO_DIR"
    git remote set-url origin https://github.com/ishan-parihar/linkedin-lyr.git
    git fetch origin
    git reset --hard origin/main
    success "Repository updated"
else
    info "Cloning repository..."
    git clone https://github.com/ishan-parihar/linkedin-lyr.git "$REPO_DIR"
    cd "$REPO_DIR"
    success "Repository cloned"
fi

# Install dependencies using uv
info "Installing dependencies with uv..."
uv sync

success "Dependencies installed"

# Create symlink for CLI (if not already exists)
CLI_PATH="$REPO_DIR/.venv/bin/linkedin-lyr"
if [ -f "$CLI_PATH" ]; then
    if [ ! -L "$HOME/.local/bin/linkedin-lyr" ]; then
        info "Creating CLI symlink..."
        mkdir -p "$HOME/.local/bin"
        ln -s "$CLI_PATH" "$HOME/.local/bin/linkedin-lyr"
        success "CLI symlink created"
    else
        info "CLI symlink already exists, updating..."
        rm "$HOME/.local/bin/linkedin-lyr"
        ln -s "$CLI_PATH" "$HOME/.local/bin/linkedin-lyr"
        success "CLI symlink updated"
    fi
else
    error "CLI binary not found at $CLI_PATH"
    exit 1
fi

# Ensure PATH includes ~/.local/bin
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    warning "$HOME/.local/bin is not in PATH. Adding to shell profile..."
    
    # Detect shell and add to appropriate profile
    SHELL_RC=""
    if [ -n "$ZSH_VERSION" ]; then
        SHELL_RC="$HOME/.zshrc"
    elif [ -n "$BASH_VERSION" ]; then
        SHELL_RC="$HOME/.bashrc"
    else
        SHELL_RC="$HOME/.profile"
    fi
    
    echo "" >> "$SHELL_RC"
    echo "# LinkedIn MCP Server" >> "$SHELL_RC"
    echo "export PATH=\"\$HOME/.local/bin:\$PATH\"" >> "$SHELL_RC"
    
    success "Added to $SHELL_RC. Please run: source $SHELL_RC"
fi

# Setup LinkedIn MCP directory
LINKEDIN_MCP_DIR="${HOME}/.linkedin-lyr"
mkdir -p "$LINKEDIN_MCP_DIR"

success "Installation directory prepared: $LINKEDIN_MCP_DIR"

# Check if cookies already exist
if [ -f "$LINKEDIN_MCP_DIR/cookies.json" ]; then
    warning "Existing cookies found at $LINKEDIN_MCP_DIR/cookies.json"
    info "You can use: linkedin-lyr status to check your session"
else
    info "No existing cookies found."
    info "To import cookies from your browser, run:"
    echo "  linkedin-lyr import"
    echo ""
    echo "Supported browsers: chrome, brave, firefox, edge, chromium, opera, vivaldi, arc"
fi

# Install AI agent skills
info "Installing AI agent skills..."
mkdir -p ~/.agents/skills

if [ -d "$REPO_DIR/.agents/skills" ]; then
    cp -r "$REPO_DIR/.agents/skills"/* ~/.agents/skills/
    success "AI agent skills installed to ~/.agents/skills/"
else
    warning "No AI agent skills found in repository"
fi

# Print installation summary
echo ""
success "=========================================="
success "LinkedIn MCP Server Installation Complete"
success "=========================================="
echo ""
info "Quick Start:"
echo "  1. Check session status: linkedin-lyr status"
echo "  2. Import cookies: linkedin-lyr import [browser]"
echo "  3. Start MCP server: linkedin-lyr mcp"
echo ""
info "For more commands: linkedin-lyr --help"
info "Documentation: https://github.com/ishan-parihar/linkedin-lyr"
echo ""
info "AI agent skills are now available in ~/.agents/skills/"

# Ask if user wants to import cookies now
if [ ! -f "$LINKEDIN_MCP_DIR/cookies.json" ]; then
    read -p "Do you want to import cookies now? (y/n): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        info "Running cookie import..."
        "$HOME/.local/bin/linkedin-lyr" import
    fi
fi

success "Installation complete!"