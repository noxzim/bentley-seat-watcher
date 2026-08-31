# Bentley seat watcher

Polls Bentley's **public** course listing every 10 minutes and sends an urgent
push notification to your phone the moment a watched section opens up.

No Workday login, no credentials, no scraping behind SSO — Bentley publishes
live seat counts at `bentleyapps.azurewebsites.net/course-listing/`, and that
page states enrollment status is "updated in real-time when the query is
submitted."

## 1. Get push notifications working (2 minutes)

1. Install **ntfy** on your phone ([iOS](https://apps.apple.com/us/app/ntfy/id1625396347) / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)).
2. Pick a topic name that nobody could guess — it is the only thing protecting
   your alerts, so treat it like a password. Something like
   `bentley-seats-nf-7fq2xk91`.
3. In the app: **+** → subscribe to that exact topic.
4. Test it:

```bash
NTFY_TOPIC=your-secret-topic-here python3 watcher.py --test-push
```

Your phone should buzz. Then set the ntfy app's notification settings for that
topic to **critical / bypass Do Not Disturb** so you don't sleep through it.

## 2. Fill in your watchlist

Edit `watchlist.json`:

```json
{
  "term": "202609",
  "interval_minutes": 10,
  "realert_after_minutes": 180,
  "ntfy_topic": "",
  "sections": ["CS 350-3", "MA 139", "FI 305-2"]
}
```

- **`sections`** — either an exact section (`CS 350-3`) or a whole course
  (`MA 139`, which alerts on *any* open section of it).
- **`term`** — `202609` is Fall 2026. Others: `202601` Spring 2026,
  `202605G` Summer 10-week.
- **`realert_after_minutes`** — if a section is still open after this long,
  you get a reminder. Set higher if it gets noisy.
- Leave `ntfy_topic` empty here and use the `NTFY_TOPIC` env var / GitHub
  secret instead, so your topic never lands in the repo.

Course codes come straight from the [public listing](https://bentleyapps.azurewebsites.net/course-listing/)
— search your department and copy the code exactly as shown (`CS 100-19`).

Department prefixes: AC, AF, CDI, MLCH, CS, DCP, EC, EMS, XD, FDS, FI, FP, FT,
MLFR, GB, GBE, GLS, GR, HC, HI, HNR, HF, IPM, ID, MLIT, MLJA, LA, LSM, MG, MK,
MA, NAS, PH, PRS, PSY, SL, SO, MLSP, ST, SA, SS, TX, TS, UX.

## 3. Run it

**Check once, no notifications sent** (good for verifying your codes match):

```bash
python3 watcher.py --dry-run
```

**Run continuously on your Mac:**

```bash
NTFY_TOPIC=your-secret-topic-here python3 watcher.py --loop
```

**Run it in the cloud (free, survives a closed laptop):**

1. Push this folder to a **private** GitHub repo.
2. Repo → Settings → Secrets and variables → Actions → New repository secret,
   named `NTFY_TOPIC`, set to your topic.
3. Actions tab → "Bentley seat watcher" → **Run workflow**.

The workflow starts one long-running job that polls every 5 minutes for 5.5
hours, then cron restarts it. This is deliberate: GitHub's scheduled triggers
are routinely 10-30 minutes late, so a cron-per-poll would poll erratically.
Only the restart is subject to that delay, not each individual check.

## Notes

- Alerts fire on Closed→Open, on a seat count going up, and on the re-alert
  timer while a section stays open.
- `state.json` holds that history and is gitignored.
- Seats can be gone within seconds during add/drop. The notification taps
  through to MyBentley — you still register in Workday yourself.
- The script sleeps 2s between departments. Don't drop `interval_minutes`
  below ~5; this is a small school server, not an API.
