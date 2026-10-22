# OOOL RFP Finder

A free bot that watches **every school district in Florida and California** (and any other state you add) for tutoring, SPED/therapy, paraprofessional, ESOL, after school/summer, and teacher/PD opportunities. It shows everything on a dashboard and emails you the new leads every weekday morning.

**Cost: $0.** Runs on GitHub Actions, dashboard on GitHub Pages, email through a free Gmail.

## How it works

Every day (it tries every hour from 1 AM to 4 PM Eastern until one run works):

1. **Finds districts** (`discover.py`). Starts from the federal list of every US school district, finds each one's website, board meeting site (BoardDocs or the district's own board page), Title I / federal programs page, and bids page. Florida and the biggest districts go first; it keeps going where it left off each day.
2. **Reads board meetings, budgets and Title I plans** (`meetings.py`). Looks back 3 months the first time, then only new and upcoming meetings. Flags:
   - **Funding**: Title I, ESSER, grants...
   - **New positions**: interventionists, tutors, teacher assistants, paras
   - **Services**: tutoring, intervention, after school, therapy, staffing
   - **Competitor**: Varsity Tutors, FEV, Kelly Education, Sylvan... (list in `config.yml`)
   - It's **Hot** when there's action next to it (approve, contract, new positions, hire, RFP). Hot ones are emailed the same day; the rest go in the Monday roundup.
3. **Checks district websites for RFPs** (`scraper.py`). District websites only, no bid portals.
4. **Emails** (`notify.py`): daily if anything is hot or there are new bids, plus a weekly roundup on Mondays.

Each district is re-checked every 3 days, so the work is spread out. Covering the whole US the first time takes about 1 to 2 weeks of daily runs; the Bot health tab shows progress.

## Files

| File | What it does |
|---|---|
| `config.yml` | States, keywords, settings. Edit this to tune it |
| `discover.py` | Finds every district, its board meetings, Title I page and bids page |
| `meetings.py` | Reads board meetings, budgets and plans for heads-ups |
| `signals.py` | Decides what counts as a heads-up and what's hot |
| `scraper.py` | Checks everything for new leads (weekdays) |
| `inbox.py` | Reads portal emails in the bot Gmail (turned off) |
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
