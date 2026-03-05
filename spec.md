# Real Estate Signal Bot — Full Autonomy Spec
**Version:** 1.0 | **Date:** 2026-03-04 | **Status:** In Development

---

## Mission

An autonomous two-sided matchmaking engine that:
1. Detects distressed property signals (price drops, stale listings)
2. Finds the listing agent for each signal property
3. Drafts a hyper-relevant, human-tone cold email via Claude AI
4. Manages the full lead lifecycle from discovery → payment → fulfillment
5. Heals itself when blocked; logs everything

---

## Folder Structure

```
real-estate-signal-bot/
├── spec.md                    # This file
├── engine.db                  # SQLite state machine (single source of truth)
├── .env.example               # All required API keys
├── requirements.txt
├── monitor.py                 # Entry point — autonomous loop (APScheduler)
├── config.py                  # Centralized config loaded from .env
│
├── src/
│   ├── db/
│   │   ├── models.py          # Schema + state machine transition logic
│   │   └── seed.py            # Target zip codes + initial config
│   │
│   ├── signals/               # SUPPLY SIDE — property signal detection
│   │   ├── base.py            # Abstract SignalAdapter interface
│   │   ├── redfin.py          # Primary: Redfin undocumented JSON API
│   │   └── zillow_stealth.py  # Fallback: Playwright-stealth scraper
│   │
│   ├── agents/                # DEMAND SIDE — find agents in signal zip codes
│   │   ├── google_maps.py     # Google Maps Places API — find agencies by zip
│   │   └── enrichment.py      # Hunter.io / Apollo.io — verify/find email
│   │
│   ├── matchmaker/            # AI ENGINE — score + draft outreach
│   │   ├── scorer.py          # Rule-based urgency score for each signal
│   │   └── drafter.py         # Claude API — 3-sentence hyper-relevant email
│   │
│   ├── outreach/              # EMAIL — send + track
│   │   ├── sender.py          # SendGrid API sender
│   │   └── tracker.py         # Pixel/link-wrap open tracking
│   │
│   ├── payments/              # STRIPE — generate links, handle webhooks
│   │   ├── stripe_client.py   # Create Payment Links via Stripe SDK
│   │   └── webhook.py         # Flask endpoint — listens for payment events
│   │
│   └── fulfillment/           # POST-PAYMENT — deliver lead package to buyer
│       └── deliver.py         # Compose + send full lead PDF/email to buyer
│
├── webhooks/
│   └── app.py                 # Flask app hosting the Stripe webhook endpoint
│
└── logs/
    ├── monitor.log            # General run log
    └── self_heal.log          # Scrape failures + recovery actions
```

---

## Lead State Machine

All leads live in `engine.db`. State transitions are one-way and enforced in `src/db/models.py`.

```
FOUND → ENRICHED → EMAILED_FREE → REPLIED → INVOICED → PAID → FULFILLED
```

| State        | Meaning                                                        |
|--------------|----------------------------------------------------------------|
| FOUND        | Signal detected (price drop / DOM > 90). Agent not yet found. |
| ENRICHED     | Agent email found and verified.                               |
| EMAILED_FREE | First outreach sent. Free lead teaser delivered.              |
| REPLIED      | Agent replied or clicked tracked link.                        |
| INVOICED     | Stripe Payment Link generated and sent.                       |
| PAID         | Stripe confirmed payment via webhook.                         |
| FULFILLED    | Full lead package emailed to buyer.                           |

---

## Scripts — Build Order

### Phase 1: Foundation
| Script | Purpose |
|--------|---------|
| `config.py` | Load all env vars. Single import for all modules. |
| `src/db/models.py` | Create tables, enforce state transitions, upsert logic. |
| `src/db/seed.py` | Seed target zip codes. |

### Phase 2: Supply Side
| Script | Purpose |
|--------|---------|
| `src/signals/base.py` | Abstract adapter — all signal sources implement this. |
| `src/signals/redfin.py` | Hit Redfin's JSON endpoints for listings by zip. Filter: price drop > 10% or DOM > 90. |
| `src/signals/zillow_stealth.py` | Playwright-stealth fallback. Auto-triggered if Redfin fails 3x. |

### Phase 3: Demand Side
| Script | Purpose |
|--------|---------|
| `src/agents/google_maps.py` | Places API: "real estate agent" near {zip}. Extract name, phone, website. |
| `src/agents/enrichment.py` | Given agent name + company domain, call Hunter.io to get verified email. |

### Phase 4: AI Matchmaker
| Script | Purpose |
|--------|---------|
| `src/matchmaker/scorer.py` | Score signal urgency (DOM weight + price drop % weight). |
| `src/matchmaker/drafter.py` | Claude API: given property + agent profile, return 3-sentence email. |

### Phase 5: Outreach
| Script | Purpose |
|--------|---------|
| `src/outreach/sender.py` | SendGrid: send email, log message ID, update lead state. |
| `src/outreach/tracker.py` | Wrap links with redirect + log clicks. Pixel for open tracking. |

### Phase 6: Payments & Fulfillment
| Script | Purpose |
|--------|---------|
| `src/payments/stripe_client.py` | Create Stripe Payment Link for a specific lead. |
| `webhooks/app.py` | Flask app: receive `checkout.session.completed`, trigger fulfillment. |
| `src/fulfillment/deliver.py` | Build lead package (PDF or structured email) and send to buyer. |

### Phase 7: Orchestration
| Script | Purpose |
|--------|---------|
| `monitor.py` | APScheduler: runs full pipeline every 30 min. Catches all exceptions, logs to self_heal.log, never dies. |

---

## Self-Healing Strategy

When a scrape fails, the system does NOT stop. Instead:

1. **Block detected** → Log to `self_heal.log` with timestamp + URL + error
2. **Retry with rotated headers** (random User-Agent from curated pool, random delay 3-8s)
3. **3 consecutive failures** → Switch from Redfin to Zillow-stealth adapter
4. **Zillow also blocked** → Log `[NEEDS_ATTENTION]` to `self_heal.log`, skip zip, continue with others
5. **All zips failing** → Send a self-notification email to operator, pause signal loop, keep webhook alive

---

## Outreach Rules ("Super Human" Guardrails)

- **Never send to the same agent twice** (deduplicated by email in DB)
- **Mention a specific property fact** — always include address + the specific signal (e.g., "just crossed 90 days")
- **No fluff words** — banned: "leverage," "unleash," "synergy," "excited to," "I hope this finds you"
- **First outreach is always free** — no payment ask until they reply or click
- **Follow-up only triggers on engagement** — reply OR tracked link click → move to REPLIED → INVOICED
- **Max 1 follow-up per lead** if no engagement after 7 days, then mark STALE and stop

---

## Email Template Pattern (Claude will generate, this is the frame)

```
Subject: [Property Address] — [Specific Signal, e.g., "93 days on market"]

Hi [First Name],

[Sentence 1: Specific observation about their listing — the signal.]
[Sentence 2: What this typically means + the data point we have ready.]
[Sentence 3: Low-friction offer — "I have [X] buyers in [zip] looking right now, happy to share if useful."]

[Name]
```

---

## API Keys Required (in .env)

| Key | Service | Used For |
|-----|---------|---------|
| `ANTHROPIC_API_KEY` | Anthropic | Email drafting |
| `GOOGLE_MAPS_API_KEY` | Google | Agent discovery |
| `APOLLO_API_KEY` | Apollo.io | Email + LinkedIn enrichment |
| `RESEND_API_KEY` | Resend | Outreach emails |
| `MAIL_FROM` | — | Sender email address |
| `MAIL_FROM_NAME` | — | Sender display name |
| `STRIPE_SECRET_KEY` | Stripe | Payment links |
| `STRIPE_WEBHOOK_SECRET` | Stripe | Webhook verification |
| `OPERATOR_EMAIL` | — | Self-notifications |
| `BASE_URL` | — | Tracking link base (your server/ngrok) |

---

## Agentic Notes (Decisions Made Autonomously)

1. **Redfin over Zillow as primary**: Redfin exposes a JSON API at `/api/home/search/...` that is significantly more stable than Zillow's HTML. We hit JSON endpoints, not HTML, making it harder to block and easier to parse.

2. **Google Maps over LinkedIn for agent discovery**: LinkedIn's scraping protection is now near-impenetrable without paid proxies. Google Maps Places API returns business name, address, phone, and website for $2/1000 calls — far more reliable.

3. **Apollo.io over Hunter.io for enrichment**: Apollo's `/people/match` API returns email + LinkedIn URL in a single call using name + company/domain. Hunter only returns email and has weaker real estate agent coverage. Apollo's free tier includes 50 exports/month; Starter ($49/mo) = 10,000 credits.

3. **APScheduler over `while True`**: A bare `while True` with `time.sleep()` dies silently on any unhandled exception. APScheduler's `BackgroundScheduler` with a `max_instances=1` job prevents overlap, and wrapping each job in a try/except means a crash in one cycle doesn't kill the process.

4. **Flask webhook over polling**: Polling Stripe for payment status every N minutes is fragile and has race conditions. A Flask webhook endpoint that listens for `checkout.session.completed` is instant and reliable. We'll use `stripe.Webhook.construct_event()` for signature verification.

5. **SendGrid over raw SMTP**: Raw SMTP (even via Gmail) gets flagged as spam at scale. SendGrid has deliverability infrastructure, bounce handling, and unsubscribe management built in. Free tier is 100 emails/day — sufficient for this MVP.

6. **Pluggable signal adapters**: Both `redfin.py` and `zillow_stealth.py` implement `base.py`'s `SignalAdapter` interface. This means adding a new data source (e.g., RentCast API) requires writing one file and changing one config value.
