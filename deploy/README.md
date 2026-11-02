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
