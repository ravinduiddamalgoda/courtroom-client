#!/bin/bash
# ============================================================
# SinLlama Courtroom Client — Raspberry Pi Setup Script
# Tested on Raspberry Pi OS (Bookworm / Bullseye / Trixie) 64-bit
# ============================================================
set -e

echo "=== [1/7] System dependencies ==="
sudo apt-get update -y
sudo apt-get install -y \
    python3 python3-venv python3-pip \
    portaudio19-dev python3-pyaudio \
    libopenblas-dev \
    i2c-tools \
    fonts-noto \
    git

echo "=== [2/7] Enable I2C interface ==="
# Enable I2C in /boot/config.txt if not already enabled
if ! grep -q "^dtparam=i2c_arm=on" /boot/firmware/config.txt 2>/dev/null && \
   ! grep -q "^dtparam=i2c_arm=on" /boot/config.txt 2>/dev/null; then
    CONFIG_FILE="/boot/firmware/config.txt"
    [ -f "$CONFIG_FILE" ] || CONFIG_FILE="/boot/config.txt"
    echo "dtparam=i2c_arm=on" | sudo tee -a "$CONFIG_FILE"
    echo "  [INFO] I2C enabled in $CONFIG_FILE — a reboot will be needed after setup."
else
    echo "  [INFO] I2C already enabled."
fi
# Load i2c-dev module now (without rebooting)
sudo modprobe i2c-dev 2>/dev/null || true
# Add current user to i2c group so no sudo is needed
sudo usermod -aG i2c "$USER" 2>/dev/null || true

echo ""
echo "  To find your LCD I2C address run:  i2cdetect -y 1"
echo "  Common addresses: 0x27 (PCF8574) or 0x3F (PCF8574A)"
echo ""

echo "=== [3/7] Create virtual environment ==="
python3 -m venv venv
source venv/bin/activate

echo "=== [4/7] Install Python packages ==="
pip install --upgrade pip
pip install -r requirements.txt

echo "=== [5/7] Download Sinhala fonts ==="
mkdir -p fonts
FONT_URL="https://github.com/notofonts/noto-fonts/raw/main/hinted/ttf/NotoSansSinhala"
curl -L -o fonts/NotoSansSinhala-Regular.ttf "${FONT_URL}/NotoSansSinhala-Regular.ttf" || \
    echo "  [WARN] Font download failed. Place NotoSansSinhala-Regular.ttf in ./fonts/ manually."
curl -L -o fonts/NotoSansSinhala-Bold.ttf    "${FONT_URL}/NotoSansSinhala-Bold.ttf" || \
    echo "  [WARN] Bold font download failed."

echo "=== [6/7] Create sessions directory ==="
mkdir -p sessions

echo "=== [7/7] Configure environment ==="
if [ ! -f .env ]; then
    cp config.env .env
fi

echo ""
echo "============================================================"
echo " Setup complete!"
echo ""
echo " IMPORTANT — check these settings in .env before running:"
echo "   DIARIZATION_API_URL  — your server URL"
echo "   SINLLAMA_API_KEY     — your API key"
echo "   LCD_I2C_ADDRESS      — confirm with: i2cdetect -y 1"
echo "   LCD_COLS / LCD_ROWS  — match your LCD (16x2 or 20x4)"
echo "   LCD_ENABLED=false    — to disable LCD if not connected"
echo ""
echo " Edit config:  nano .env"
echo ""
echo " To run the app:"
echo "   source venv/bin/activate"
echo "   source .env && python app.py"
echo ""
echo " NOTE: If I2C was just enabled, reboot first:"
echo "   sudo reboot"
echo "============================================================"
