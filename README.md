# Ghost Translator

Синхронизация постов между двумя сайтами на [Ghost](https://ghost.org/): публикуете на источнике (RU) → на целевом сайте (EN) появляется **черновик** с переводом через [DeepL](https://www.deepl.com/pro-api).

Один Python-файл (`app.py`), Ghost Admin API, фоновый перевод после webhook. URL сайтов задаются только через переменные окружения.

## Быстрый старт

```bash
git clone https://github.com/Marfa/ghost_translator.git
cd ghost_translator
cp .env.example .env   # укажите URL и ключи
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8080
```

Docker: `mkdir -p data && docker compose up -d --build`

### Что нужно заранее

1. **Admin API keys** на обоих Ghost-сайтах: Settings → Integrations → Add custom integration
2. **DeepL API key**: [deepl.com/pro-api](https://www.deepl.com/pro-api)
3. **Публичный HTTPS-URL** для webhook

## Webhook

На **исходном Ghost-сайте** → Settings → Integrations → Add webhook:

| Event | URL |
|-------|-----|
| `post.published` | `https://ВАШ-ХОСТ/webhook/ghost` |
| `post.published.edited` | тот же URL |

**Secret** = значение `WEBHOOK_SECRET` из `.env`.

Ghost ждёт ответ ~2 секунды; сервис сразу отвечает `200 OK` и переводит пост в фоне.

В черновик на целевом сайте копируются: заголовок, slug, HTML (без блока тегов `#…` внизу), excerpt, meta/OG, X card, обложка. **Теги не копируются** — в API уходит `tags: []`, ссылки `/tag/…` вырезаются из HTML (иначе Ghost создаёт их при `source=html`). X card description берётся из `twitter_description`, а если пусто — из excerpt/meta.

### Пропущенный пост (webhook не дошёл)

Если сервис спал или webhook потерялся:

```bash
curl -X POST "https://ВАШ-ХОСТ/sync/POST_ID" \
  -H "X-Sync-Secret: ВАШ_WEBHOOK_SECRET"
```

`POST_ID` — id поста на исходном Ghost (из URL редактора). Ответ сразу `200`, перевод идёт в фоне.

### Автосверка пропущенных постов

Вместо постоянного пинга (UptimeRobot) можно раз в несколько часов будить Render и сверять список опубликованных постов с `map.json`:

```bash
curl -X POST "https://ВАШ-ХОСТ/reconcile" \
  -H "X-Sync-Secret: ВАШ_WEBHOOK_SECRET"
```

Переводятся только посты **опубликованные за последние 24 часа**, которых нет в `map.json`. Уже синхронизированные пропускаются; если `map.json` сбросился, черновик ищется по slug на целевом сайте.

**GitHub Actions** (файл `.github/workflows/reconcile.yml`): в Secrets репозитория задайте `RECONCILE_URL` = `https://ваш-сервис.onrender.com/reconcile` и `WEBHOOK_SECRET`. Workflow запускается каждые 6 часов и вручную (Actions → Run workflow). UptimeRobot при этом не обязателен.

## Переменные окружения

| Переменная | Назначение |
|------------|------------|
| `SOURCE_GHOST_URL` | URL исходного (русского) сайта |
| `SOURCE_GHOST_ADMIN_API_KEY` | Admin API key источника |
| `TARGET_GHOST_URL` | URL целевого (английского) сайта |
| `TARGET_GHOST_ADMIN_API_KEY` | Admin API key цели |
| `DEEPL_API_KEY` | Перевод RU→EN |
| `WEBHOOK_SECRET` | Подпись webhook и заголовок `X-Sync-Secret` |
| `MAP_FILE` | Маппинг source→target post id (по умолчанию `map.json`) |

## Проверка

```bash
curl https://ВАШ-ХОСТ/health
# {"status":"ok"}
```

Опубликуйте пост на исходном сайте — на целевом появится draft.

## Деплой бесплатно

### Render (проще всего)

1. [render.com](https://render.com) → **New → Blueprint** → репозиторий [Marfa/ghost_translator](https://github.com/Marfa/ghost_translator)
2. Заполните переменные окружения (включая `SOURCE_GHOST_URL` и `TARGET_GHOST_URL`)
3. Webhook: `https://ghost-translator-xxxx.onrender.com/webhook/ghost`

**UptimeRobot** опционален: webhook сработает сразу, если сервис не спит; иначе `/reconcile` по cron (см. выше) подхватит пропуски.

### Oracle Cloud Always Free

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

## Авторство

Код разработан с помощью [Cursor](https://cursor.com).
