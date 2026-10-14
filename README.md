# OOOL RFP Finder

A free bot that watches **every school district in Florida and California** (and any other state you add) for tutoring, SPED/therapy, paraprofessional, ESOL, after school/summer, and teacher/PD opportunities. It shows everything on a dashboard and emails you the new leads every weekday morning.

**Cost: $0.** Runs on GitHub Actions, dashboard on GitHub Pages, email through a free Gmail.

## How it works

It has 4 layers, so if one misses something, another catches it:

1. **Every district's bids page.** Once a month, `discover.py` pulls the official federal list of every district, finds each one's website, then hunts down their purchasing/bids page, the bid portal they use, and their board meeting site. Every weekday, `scraper.py` checks all of those pages and opens the bid PDFs to read what's inside (a lot of RFPs hide "tutoring" in Lot 5 of a 30-page PDF).
2. **Bid portal email alerts.** Hundreds of districts post on the same few portals (DemandStar, BidNet, PlanetBids, Bonfire...). The bot Gmail is signed up for free alerts on those, and the bot reads that inbox every morning.
3. **Google Alerts.** Also delivered to the bot Gmail, so news and mentions anywhere on the web get caught.
4. **Board meetings** (optional, off by default, see the bottom).

Everything gets scored by keywords in `config.yml`, tagged by service (Tutoring, Special Ed & Therapy, etc.), and put on the dashboard. New stuff gets emailed.

## Files

| File | What it does |
|---|---|
| `config.yml` | States, keywords, settings. Edit this to tune it |
| `discover.py` | Finds every district and its bids page (monthly) |
| `scraper.py` | Checks everything for new leads (weekdays) |
| `inbox.py` | Reads portal + Google Alert emails in the bot Gmail |
| `notify.py` | Sends the email |
| `common.py` | Shared code |
| `index.html` | The dashboard |
| `sources.csv` | Extra websites you add by hand |
| `district_overrides.csv` | Fix a district's link by hand when the bot gets it wrong |
| `sources_auto.csv` | Made by the bot, don't edit |
| `data/` | Results, made by the bot |

---

## Setup checklist

### 1. Bot Gmail (5 min)

1. Make a new Gmail just for the bot, like `ooolrfpfinder@gmail.com`.
2. Go to myaccount.google.com, click **Security**, turn on **2-Step Verification**.
3. In the search bar at the top of that page, type **App passwords**. Create one named `RFP Finder`. Copy the 16-letter password (no spaces).

### 2. Secrets in GitHub (3 min)

In the repo: **Settings**, then **Secrets and variables**, then **Actions**, then **New repository secret**. Add these one at a time:

| Name | Value |
|---|---|
| `SMTP_USER` | the bot Gmail address |
| `SMTP_PASS` | the 16-letter app password |
| `EMAIL_TO` | `msuarez@oneononelearning.com` |
| `DASHBOARD_URL` | the dashboard link (`https://ONEONONELEARNING.github.io/rfp-finder/`, all lowercase) |

To add the CEO or team later, edit `EMAIL_TO` and separate emails with commas.

### 3. Sign the bot Gmail up for free alerts (20 to 30 min, one time)

Use the bot Gmail for all of these. When it asks what you sell, pick education, tutoring, staffing, special education, and therapy categories. Turn on email notifications.

| Portal | Covers |
|---|---|
| **DemandStar** (demandstar.com) | Lots of FL districts (Broward and more) |
| **BidNet Direct / Florida Purchasing Group** (bidnetdirect.com) | Many FL districts + CA agencies |
| **VendorLink** (vendorlink.org) | Several FL districts |
| **PlanetBids** | Many CA districts (each district has its own vendor portal, register on the big ones) |
| **Bonfire** (bonfirehub.com) | Many CA districts |
| **Procureware** | Fresno Unified and others |
| **MyFloridaMarketPlace** | Florida state agencies |
| **Cal eProcure** (caleprocure.ca.gov) | California state agencies |

Tip: the dashboard's **Coverage** tab shows which portal each district uses, so you know which ones matter most.

### 4. Google Alerts (5 min)

Signed into the bot Gmail, go to google.com/alerts. For each search below, click **Show options**, set **How often** to *At most once a day*, **Deliver to** the bot Gmail, then **Create Alert**:

- `tutoring ("request for proposals" OR RFP OR solicitation) "school district" Florida`
- `tutoring ("request for proposals" OR RFP OR solicitation) "school district" California`
- `"high-dosage tutoring" school board Florida`
- `"high-dosage tutoring" school board California`
- `("paraprofessional" OR "instructional assistant") staffing RFP school district`
- `("speech language" OR "occupational therapy" OR "behavior analyst") services RFP school district`
- `"expanded learning" OR "ELO-P" RFP California`
- `"after school" OR "summer learning" provider RFP school district Florida`

### 5. Run it

**Actions** tab, then **RFP Finder (daily)**, then **Run workflow**. The first run takes longer (maybe 1 to 2 hours) because it finds every district first. After that, daily runs take about 20 to 40 minutes.

---

## Fixing a district the bot got wrong

Open the dashboard **Coverage** tab. Anything not marked **found** needs a quick look:

1. Google `[district name] purchasing bids` and copy the page that lists their solicitations.
2. On GitHub, open `district_overrides.csv`, click the pencil, add a line like:
   `FL,Suwannee,https://www.suwannee.k12.fl.us/,https://www.suwannee.k12.fl.us/purchasing,,`
   (state, part of the district name, website, bids page, board meeting link, notes)
3. Commit. It gets picked up next run.

## Adding more states

In `config.yml`, change `states: [FL, CA]` to something like `states: [FL, CA, TX, GA]`. Then go to **Actions**, **Find districts (monthly)**, **Run workflow**.

## Tuning

`config.yml` has every keyword, grouped by service. Too much junk? Add words to `negative` or raise `min_score`. Missing stuff? Add keywords.

## Board meetings (BoardDocs)

Board meetings are the early warning (a board approving tutoring money usually comes before the RFP). Most districts use a site called BoardDocs. BoardDocs has a file on their site (robots.txt) that says "please, no bots." It's not a law, it's more like a "please don't" sign, and this bot follows those signs by default.

If your VP is OK with the bot checking public agendas a few times a day anyway, open `config.yml` and change `check_boarddocs: false` to `true`. That's it. The Google Alerts in step 4 catch a lot of board news in the meantime.

## If something breaks

- **Red X in Actions:** click it, click the failed step to see why. One bad site never stops the rest.
- **Sources tab shows Error:** that site moved or is down. Fix it in `district_overrides.csv` if it keeps happening.
- **No email:** normal on days with nothing new.
- **Schedule stopped:** GitHub pauses schedules on repos with no activity for 60 days. The bot's daily saves count as activity, but if it happens, go to Actions and click **Enable workflow**.
