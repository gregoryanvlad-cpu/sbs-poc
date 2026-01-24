# SBS PoC (structured skeleton)

Это PoC-версия Telegram-бота "СБС" с нормальной структурой проекта:
- `app/bot/...` роутеры и UI
- `app/services/...` доменные сервисы (VPN пока mock)
- `app/db/...` ORM-модели + Alembic миграции
- `app/scheduler/...` scheduler jobs (advisory lock в Postgres)

Функционально сейчас:
- подпиской (календарный +1 месяц на каждую "успешную оплату" в mock-режиме)
- VPN-модулем (пока MOCK: выдаём WireGuard-конфиг, QR, сброс)
- scheduler каждые 30 секунд (окончание подписки → выключает VPN)

## Миграции

Схема БД управляется Alembic.

Локально/на сервере:
```bash
export DATABASE_URL=postgres://...
alembic upgrade head
```

В Docker-контейнере миграции запускаются автоматически (`start.sh`).

## ENV переменные

Обязательные:
- BOT_TOKEN
- DATABASE_URL (Railway: ${{Postgres.DATABASE_URL}})

Рекомендуемые:
- TZ=Europe/Moscow
- DEBUG=0

VPN (пока для конфига):
- VPN_ENDPOINT=1.2.3.4:51820
- VPN_SERVER_PUBLIC_KEY=REPLACE_ME
- VPN_ALLOWED_IPS=0.0.0.0/0, ::/0
- VPN_DNS=1.1.1.1,8.8.8.8

Флаги:
- VPN_MODE=mock
- SCHEDULER_ENABLED=1

## Миграции (Alembic)

Локально:
```bash
export DATABASE_URL=postgres://...
alembic upgrade head
```

На Railway миграции запускаются при старте контейнера (см. `start.sh`).

## Как обновлять код в GitHub (проще всего)

### Вариант 1 (через GitHub Web)
1) Открой свой репозиторий → **Add file** → **Upload files**
2) Перетащи содержимое архива (папки/файлы) в окно
3) Убедись что структура сохранилась: `app/main.py`, `requirements.txt`, `Dockerfile`
4) Нажми **Commit changes**

### Вариант 2 (через git локально)
1) Скачай репозиторий:
   ```bash
   git clone <URL_твоего_репо>
   cd <папка_репо>
   ```
2) Удали старые файлы проекта (НЕ трогай папку .git) и распакуй сюда архив.
3) Закоммить:
   ```bash
   git add .
   git commit -m "Update PoC: VPN module + scheduler"
   git push
   ```

После пуша Railway сам задеплоит изменения.

## ВАЖНО
Это PoC: реальные WireGuard peer'ы на сервере пока не создаются.
Следующий шаг — подключить реальный VPN provisioner на VPS.
