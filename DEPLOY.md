# ERPNext Telegram bot — serverga qo'yish (Ubuntu 24.04)

Bot **long-polling** ishlatadi: serverga **domen, ochiq port yoki SSL kerak emas**.
Faqat chiquvchi internet (Telegram + Anthropic + ERPNext'ga ulanish) kifoya.

## Server talablari
- **Tavsiya:** 1–2 vCPU, 2 GB RAM, 20–25 GB SSD, Ubuntu 24.04 LTS.
- Eng arzon VPS tarif yetadi (og'ir ish Anthropic/ERPNext serverida bo'ladi).
- Anthropic API (`api.anthropic.com`) va Telegram'ga chiqish bloklanmaganini tekshiring.
  Mahalliy tarmoqda muammo bo'lsa, xalqaro VPS (Hetzner/DigitalOcean/Vultr) tavsiya etiladi.

## 1. Serverni tayyorlash
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip
sudo useradd -m -s /bin/bash erpbot      # bot uchun alohida foydalanuvchi
```

## 2. Fayllarni ko'chirish
O'z kompyuteringizdan **faqat shu 4 faylni** ko'chiring (`.venv`, `__pycache__` ni EMAS):
```bash
scp agent.py .env requirements.txt erpbot.service erpbot@SERVER_IP:/home/erpbot/
```

## 3. Virtual muhit va kutubxonalar
```bash
sudo -iu erpbot
cd ~
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
chmod 600 ~/.env          # maxfiy kalitlar faylini himoyalaymiz
```

## 4. Qo'lda sinab ko'rish
```bash
.venv/bin/python agent.py
```
`Bot ishga tushdi` chiqsa, Telegram'dan botga yozib tekshiring, keyin `Ctrl+C`.

## 5. 24/7 ishlashi uchun systemd
`erpbot.service` ichidagi yo'llar `/home/erpbot/...` ga to'g'ri kelsa, shundayligicha qo'ying
(boshqa foydalanuvchi/yo'l bo'lsa `User=` va yo'llarni tahrirlang):
```bash
exit                                                   # erpbot'dan chiqib, sudo'li foydalanuvchiga
sudo cp /home/erpbot/erpbot.service /etc/systemd/system/erpbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now erpbot
sudo systemctl status erpbot      # holati
journalctl -u erpbot -f           # loglarni real vaqtda kuzatish
```
Endi bot doim ishlaydi, server qayta yuklansa avtomatik ko'tariladi, xatoda qayta uriladi.

## 6. Yangilash (kod o'zgarsa)
```bash
scp agent.py erpbot@SERVER_IP:/home/erpbot/
sudo systemctl restart erpbot
```

## 7. (Tavsiya) Xavfsizlik
```bash
sudo ufw allow OpenSSH && sudo ufw enable   # faqat SSH ochiq
```
**Maxfiy kalitlar:** `.env` ichida haqiqiy Anthropic kaliti, Telegram tokeni va ERPNext
kalitlari bor. Agar bu fayl avval boshqa joyda ochiq turgan bo'lsa, kalitlarni
almashtiring (Anthropic: console.anthropic.com; Telegram: @BotFather → /revoke).

---
### .env formati (namuna)
Qiymatlar Python sintaksisida — matn `"..."` ichida, `ALLOWED_USERS` ro'yxat:
```
ERP_URL       = "https://erp.example.com"
ERP_KEY       = "xxxxxxxxxxxxxxx"
ERP_SECRET    = "xxxxxxxxxxxxxxx"
ANTHROPIC_KEY = "sk-ant-..."
MODEL         = "claude-haiku-4-5-20251001"
TG_TOKEN      = "123456:ABC-..."
COMPANY       = "Kompaniya nomi"
ALLOWED_USERS = [123456789, 987654321]
```
`MODEL`/sozlamani systemd dan ham berish mumkin (environment .env dan ustun turadi).
