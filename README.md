# Abonix Social

The daily social media agent for Abonix. Every morning it writes a Romanian
post with Claude, builds the image from a brand template (real app screenshots,
or a stock photo from Pixabay on Thursdays), and sends it to you on Telegram. You approve,
regenerate, or skip. Approved posts go to the Abonix Facebook Page and Instagram
account at the publish time.

It runs on the Abonix server as its own Docker container and **touches nothing
in Abonix**: no shared database, network, Nginx or ports. Telegram is polled
outbound, images are hosted on Cloudflare R2 (`media.abonix.net`), and posts go
straight to the Meta Graph API.

```
08:00  calendar theme -> Claude (text, JSON) -> rule checks + Claude review
       -> image (screenshot / stock photo) -> HTML template -> Playwright -> JPEG
       -> Telegram: ✅ Aprob  ⏭ Sar peste  🔄 Regenerează  🖼 Altă imagine
                    (or reply with instructions: "titlul mai scurt")
18:30  approved -> upload to R2 -> Instagram (container -> publish) + Facebook
       retries 10 min / 30 min / 2 h, then alerts you; never publishes twice
```

## Weekly calendar (`app/content/calendar.yaml`)

| Day | Theme | Template |
|---|---|---|
| Mon | One Abonix feature | Real screenshot in a browser or phone frame |
| Tue | Practical tip for owners | Big quote |
| Wed | Excel/notebook vs Abonix | Split |
| Thu | For one type of club | Stock photo (Pixabay) + headline |
| Fri | A plan and its price | Offer card (price taken from `product.yaml`) |
| Sat | A number from the product | Big number (only from `facts.yaml`) |
| Sun | No post | |

Subjects rotate, so the least recently used one comes next. The last 30 posts
go into the prompt so angles don't repeat.

## Content files (edit these, no code)

- `app/content/brand.md`: tone, banned words, what Abonix does **not** do, CTAs.
- `app/content/product.yaml`: plans, prices and features. This is the only
  source of truth, and any RON amount not listed here is flagged.
- `app/content/facts.yaml`: the only numbers allowed on "big number" posts.
- `app/content/calendar.yaml`: themes, guidance and rotation subjects.
- `assets/screens/`: real screenshots, copied from Abonix `frontend/public/tour/`.
  Refresh them when the app changes.

### Keeping the product sheet current
Claude only writes about what's in `product.yaml`, so a new Abonix feature
appears in posts only once it's added there. Check after Abonix releases:

```bash
.venv/Scripts/python -m app.cli sync-check     # expects the Abonix repo at ../Abonix (or --abonix PATH)
```

It compares prices and plan limits with Abonix's `pricing.json`, which plan has
which feature, and the tour screenshots, then lists the Abonix commits since the
sheet was last checked. It only reports and changes nothing. Add new features to
`product.yaml` by hand (and to the Monday `subjects` in `calendar.yaml`), copy
new screenshots, then run `sync-check --mark` to record the Abonix commit the
sheet now matches.

## Setup

### 1. Telegram (5 minutes)
The bot only obeys chats whose id is in `TELEGRAM_ALLOWED_CHAT_IDS`. You don't
know your id yet, so you start the bot once without it and it tells you.

1. **Create the bot.** In Telegram, message **@BotFather** → `/newbot` → pick a
   name (e.g. *Abonix Social*) and a username ending in `bot`. BotFather replies
   with a token like `123456:ABC-...` and a link `t.me/<username>`.
   In `.env`, set `TELEGRAM_BOT_TOKEN=<that token>` and leave
   `TELEGRAM_ALLOWED_CHAT_IDS` empty.
2. **Get your chat id.** Start the agent (`docker compose up -d --build`, see
   *Run on the server*). Open the `t.me/<username>` link on your phone and tap
   **Start**. The bot replies:
   *"Salut! ID-ul acestui chat este 123456789…"*. That number is your chat id.
   (Without starting the agent: message your bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the
   number after `"chat":{"id":`.)
3. **Allow yourself.** In `.env`, set `TELEGRAM_ALLOWED_CHAT_IDS=123456789`
   (several people: comma-separated). Then run `docker compose up -d --force-recreate`. Use this,
   **not** `docker compose restart`, which keeps the old `.env`. Send `/status`
   to the bot. If it answers, you're done.

### 2. Claude API key
The pipeline needs a Claude **API key**. A claude.ai Pro/Max subscription does
not include API access; the API is billed separately, pay as you go.
1. Go to **platform.claude.com** (formerly console.anthropic.com). You can sign
   in with your claude.ai account.
2. *Billing*: add some credit ($5 is plenty) and set a monthly spend limit of about $5.
3. *API keys* → *Create key* → `ANTHROPIC_API_KEY`.

Defaults: `claude-sonnet-5-5` writes, `claude-haiku-5-5` reviews.

### 3. Stock photos (Thursday posts only)
1. Sign up at **pixabay.com**, then open https://pixabay.com/api/docs/. Your key
   is shown in the *Parameters* section once you're logged in → `PIXABAY_API_KEY`.
2. That's it. Photos are downloaded and credited ("Foto: name / Pixabay"),
   as Pixabay's terms ask.

If you already have a Pexels key, `PEXELS_API_KEY` still works (Pexels has
paused new keys). Without either key, everything except the Thursday post works.

### 4. Cloudflare R2 (image hosting for Meta)
1. Cloudflare dashboard → **R2** → *Create bucket* `abonix-social`.
2. Bucket → *Settings* → *Custom Domains* → *Connect domain* → `media.abonix.net`.
   Cloudflare creates the DNS record. This specific record wins over the
   `*.abonix.net` wildcard, so Abonix is not affected.
3. R2 → *Manage API tokens* → *Create API token* → **Object Read & Write**,
   limited to this bucket. Copy the Access Key ID, Secret Access Key and your Account ID into `R2_*`.

### 5. Meta (plan step 1)
1. Instagram @abonixnet → switch to a **Business** account and link it to the Facebook Page.
2. **Meta Business Suite / Business Manager** must own the Page and the IG account.
3. developers.facebook.com → *Create app* → type **Business** → attach it to the Business Manager.
4. Business settings → *Users → System users* → add one (role **Admin**) →
   *Assign assets*: the Page (full control) and the Instagram account.
5. *Generate token* for the app, with expiry **Never** and the permissions
   `pages_show_list, pages_read_engagement, pages_manage_posts, instagram_basic,
   instagram_content_publish, business_management` → `META_ACCESS_TOKEN`.
6. Run `python -m app.cli meta-check`. It prints the Page and the
   `instagram_business_account` id. Put them into `META_PAGE_ID` / `META_IG_USER_ID`.
7. If the Page requires two-factor authentication, the person who generated
   the token must have it enabled, or `media_publish` fails.

## Run on the server

```bash
git clone <this repo> /opt/abonix-social && cd /opt/abonix-social
cp .env.example .env    # fill in; keep DRY_RUN=true at first
docker compose up -d --build
docker compose logs -f
```

Going live, in order:
1. With `DRY_RUN=true`, approve a few drafts in Telegram. They are marked "published (DRY RUN)" and nothing is sent.
2. `docker compose exec social python -m app.cli meta-check` passes.
3. `docker compose exec social python -m app.cli meta-test /app/assets/...jpg` posts one test image (it asks first), then delete it.
4. Set `DRY_RUN=false` and run `docker compose up -d --force-recreate`.

## Deploying updates
Deploys only rebuild and restart the `abonix-social` container. Abonix's own
containers are never stopped, rebuilt or pruned, and the script refuses to run
in a folder whose `docker-compose.yml` isn't `name: abonix-social`.

- **From GitHub:** *Actions → Deploy → Run workflow* (branch `main`). Needs the
  repo secrets `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY` (same values as
  the Abonix repo) and `DEPLOY_PATH=/opt/abonix-social`.
- **On the server:** `bash /opt/abonix-social/scripts/deploy.sh`

Either way it fetches `main`, rebuilds, recreates the container (so `.env`
changes apply) and waits until the agent has started, or prints the last log
lines if it hasn't. `data/` and `.env` are git-ignored and never touched.
Pushing to GitHub deploys nothing by itself.

**CI** (`.github/workflows/ci.yml`) runs the tests on every push to `main` and
on every pull request, with no API keys. Check that it's green before pressing Deploy.

## Changing `.env`
After any edit to `.env`, run `docker compose up -d --force-recreate`.
`docker compose restart` (and sometimes plain `up -d`) keeps the old values.

## Telegram commands
`/azi` generate today's draft now · `/genereaza 2026-11-12` ·
`/extra` one more post today, even if today's is already approved or published ·
`/extra joi` an extra post with another day's theme (luni … sâmbătă) · `/status` ·
`/pauza` / `/reia` stop/resume automation · `/verifica` Meta token + IG quota ·
reply to a draft with text → regenerate with those instructions.

## Local development

```bash
py -3.12 -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt
.venv/Scripts/python -m playwright install chromium
.venv/Scripts/python -m pytest              # no keys needed
.venv/Scripts/python -m app.cli samples     # renders every template into data/samples/
.venv/Scripts/python -m app.cli draft --date 2026-11-09   # one real draft (needs Claude, plus Pixabay for Thursdays)
.venv/Scripts/python -m app.cli week        # 7 drafts, plan step 3 check
```

## Costs (one post a day)
Claude: roughly 30 × (writing + review + occasional fix pass), about $1/month at
list prices. Pixabay: free. R2: free tier. Meta API: free.

## Later (plan step 8)
Carousels, reels (3–5 rendered frames + ffmpeg), daily Insights, an AI image
provider behind `ImageProvider`, and auto-publishing without approval once
the drafts are consistently good.
