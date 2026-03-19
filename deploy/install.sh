#!/bin/bash
# PolySignal Kurulum Script'i — CloudPanel sunucusunda calistir
set -e

echo "━━ PolySignal Kurulum ━━"

# 1. Repo klonla
cd /home
if [ -d "polysignal" ]; then
    echo "Guncelleniyor..."
    cd polysignal && git pull
else
    echo "Klonlaniyor..."
    git clone https://github.com/Ninetheme/polysignal.git
    cd polysignal
fi

# 2. Bagimliliklar
echo "Python paketleri kuruluyor..."
pip3 install python-dotenv websockets aiohttp certifi requests

# 3. Data klasoru
mkdir -p data

# 4. Systemd servisi
echo "Servis kuruluyor..."
cp deploy/polysignal.service /etc/systemd/system/polysignal.service
systemctl daemon-reload
systemctl enable polysignal
systemctl restart polysignal

# 5. Nginx config
echo "Nginx yapilandiriliyor..."
cp deploy/nginx-bot.conf /etc/nginx/sites-enabled/bot.ninetheme.com.conf
nginx -t && systemctl reload nginx

# 6. SSL (CloudPanel'den yapilmadiysa)
# certbot --nginx -d bot.ninetheme.com

echo ""
echo "━━ KURULUM TAMAMLANDI ━━"
echo "Bot: https://bot.ninetheme.com"
echo "Servis: systemctl status polysignal"
echo "Log: journalctl -u polysignal -f"
