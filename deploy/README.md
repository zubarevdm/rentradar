# Деплой на сервер

1. Остановить бота на старом ПК (два экземпляра на одном токене конфликтуют).
2. Скопировать на сервер `data/rentradar.db`, `.env`, `profiles.yaml`.
3. На сервере: `git clone` → `python3 -m venv .venv && .venv/bin/pip install -e .`
4. Проверка Telegram: `curl -m 10 https://api.telegram.org` — если таймаут, нужен relay.

## Relay через Cloudflare Worker

1. dash.cloudflare.com → Workers → Create → вставить `tg-relay-worker.js` → Deploy.
2. Settings → Variables: `BOT_ID` = число до «:» в токене бота.
3. В `.env` на сервере: `RENTRADAR_TELEGRAM_API_BASE=https://<имя>.<аккаунт>.workers.dev`
4. Проверка: `curl https://<имя>.<аккаунт>.workers.dev/bot<токен>/getMe` → `"ok":true`.

Если `*.workers.dev` тоже недоступен с сервера — привязать свой домен к воркеру
(Workers → Triggers → Custom Domains).

## systemd

`/etc/systemd/system/rentradar.service`:

```
[Unit]
Description=RentRadar
After=network-online.target

[Service]
WorkingDirectory=/opt/rentradar
ExecStart=/opt/rentradar/.venv/bin/rentradar serve
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

`systemctl daemon-reload && systemctl enable --now rentradar && journalctl -u rentradar -f`

Бэкап БД раз в сутки (crontab):
`0 4 * * * sqlite3 /opt/rentradar/data/rentradar.db ".backup /opt/rentradar/backup/db-$(date +\%F).db"`

## Авито через телефон (мобильный прокси)

IP сервера Авито банит; ходим через Android-телефон с SIM (Tailscale exit node).

- Телефон: Tailscale → «Run as exit node»; в админке Tailscale у узла — «Use as exit node».
  Один раз по USB: `adb tcpip 5555` (повторять после перезагрузки телефона).
- Сервер: в `/etc/default/tailscaled`
  `FLAGS="--tun=userspace-networking --socks5-server=127.0.0.1:1055 --outbound-http-proxy-listen=127.0.0.1:1056"`,
  затем `tailscale set --exit-node=<IP телефона>` — через телефон идёт только трафик прокси.
- `phone-adb.service`: `socat` 127.0.0.1:15555 → `tailscale nc <IP телефона> 5555`.
- `/usr/local/bin/phone-rotate-ip`: режим полёта на 4 с через adb → новый IP.
- `.env`: `RENTRADAR_AVITO_PROXY=http://127.0.0.1:1056`, `RENTRADAR_AVITO_BROWSER_WARMUP=true`,
  `RENTRADAR_AVITO_ROTATE_CMD=/usr/local/bin/phone-rotate-ip`.
- Проверка: `curl -x http://127.0.0.1:1056 https://api.ipify.org` → IP оператора.
