# Ghost Translator

**Опубликовали на русском Ghost — на английском появится черновик с переводом.**

Синхронизация двух сайтов [Ghost](https://ghost.org/) через [DeepL](https://www.deepl.com/pro-api): один Python-файл (`app.py`), webhook, фоновый перевод.

```bash
git clone https://github.com/Marfa/ghost_translator.git
cd ghost_translator
cp .env.example .env
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8080
```

Docker: `mkdir -p data && docker compose up -d --build`

## Возможности

| Что | Как |
|-----|-----|
| Автоперевод | Webhook `post.published` на `/webhook/ghost` |
| Пропущенный пост | `POST /sync/{id}` с заголовком `X-Sync-Secret` |
| Автосверка | `POST /reconcile` — посты за последние 24 ч, без map |
| Длинные статьи | HTML режется на части (лимит DeepL 128 KiB) |

## Что копируется в черновик

| Поле | Примечание |
|------|------------|
| Заголовок, slug, HTML | Теги `#…` и ссылки `/tag/…` вырезаются |
| Excerpt, meta/SEO | |
| Facebook card | `og_title`, `og_description`, `og_image` |
| X card | `twitter_title`, `twitter_description`, `twitter_image` |
| Обложка | `feature_image`, alt |
| Теги поста | **не копируются** (`tags: []`) |

Card description берётся из своего поля; если в API пусто — из excerpt или meta.

## Быстрый старт

### Что нужно заранее

1. **Admin API keys** на обоих Ghost: Settings → Integrations → Add custom integration
2. **DeepL API key**: [deepl.com/pro-api](https://www.deepl.com/pro-api)
3. **Публичный HTTPS-URL** для webhook

### Webhook

На **исходном Ghost** → Settings → Integrations → Add webhook:

| Event | URL |
|-------|-----|
| `post.published` | `https://ВАШ-ХОСТ/webhook/ghost` |
| `post.published.edited` | тот же URL |

**Secret** = `WEBHOOK_SECRET` из `.env`. Ghost ждёт ответ ~2 с; сервис сразу отвечает `200 OK` и переводит в фоне.

### Пропущенный пост

```bash
curl -X POST "https://ВАШ-ХОСТ/sync/POST_ID" \
  -H "X-Sync-Secret: ВАШ_WEBHOOK_SECRET"
```

`POST_ID` — id поста в URL редактора Ghost.

### Автосверка (вместо UptimeRobot)

```bash
curl -X POST "https://ВАШ-ХОСТ/reconcile" \
  -H "X-Sync-Secret: ВАШ_WEBHOOK_SECRET"
```

Переводятся published-посты **за последние 24 часа**, которых нет в `map.json`. Если map сбросился — черновик ищется по slug.

**GitHub Actions** (`.github/workflows/reconcile.yml`): Secrets → `RECONCILE_URL` = `https://ваш-хост/reconcile`, `WEBHOOK_SECRET`. Запуск каждые 6 ч или вручную: Actions → Reconcile missed posts → Run workflow.

## Переменные окружения

| Переменная | Назначение |
|------------|------------|
| `SOURCE_GHOST_URL` | URL исходного (RU) сайта |
| `SOURCE_GHOST_ADMIN_API_KEY` | Admin API key источника |
| `TARGET_GHOST_URL` | URL целевого (EN) сайта |
| `TARGET_GHOST_ADMIN_API_KEY` | Admin API key цели |
| `DEEPL_API_KEY` | Перевод RU→EN |
| `WEBHOOK_SECRET` | Подпись webhook и `X-Sync-Secret` |
| `MAP_FILE` | Маппинг source→target id (по умолчанию `map.json`) |

## Проверка

```bash
curl https://ВАШ-ХОСТ/health
```

Опубликуйте пост на RU — на EN появится draft.

## Деплой на VPS (HostKey)

Прод: Docker Compose на HostKey VPS + nginx + Let's Encrypt.

```bash
# на сервере
git clone https://github.com/Marfa/ghost_translator.git /opt/ghost-translator
cd /opt/ghost-translator
cp .env.example .env   # заполнить секреты
mkdir -p scripts && chmod +x scripts/deploy_vps.sh
./scripts/deploy_vps.sh
```

Обновление после push в `main`:

```bash
/opt/ghost-translator/scripts/deploy_vps.sh
```

Nginx проксирует публичный HTTPS на контейнер (`127.0.0.1:8082`). Webhook / Actions:

`https://ВАШ-ДОМЕН/webhook/ghost` и `https://ВАШ-ДОМЕН/reconcile`.

### Локально / Oracle Cloud Always Free

```bash
git clone https://github.com/Marfa/ghost_translator.git && cd ghost_translator
cp .env.example .env && mkdir -p data
docker compose up -d --build
```

### Cloudflare Tunnel (локально)

```bash
uvicorn app:app --port 8080
cloudflared tunnel --url http://localhost:8080
```

## Лицензия

[CC BY-NC-SA 4.0 International](https://creativecommons.org/licenses/by-nc-sa/4.0/) — см. [LICENSE](LICENSE).

Некоммерческое использование; производные работы — с тем же лицензированием; указание авторства обязательно.

## Авторство и поддержка

Код подготовлен с помощью [Cursor](https://cursor.com).

[![Donate](https://img.shields.io/badge/Donate-DonationAlerts-orange)](https://www.donationalerts.com/r/themarfa)
[![Crypto](https://img.shields.io/badge/Crypto-NOWPayments-blue)](https://nowpayments.io/donation/themarfa)

Поддержка проекта:

- [DonationAlerts](https://www.donationalerts.com/r/themarfa)
- [Донат криптой (NOWPayments)](https://nowpayments.io/donation/themarfa)
