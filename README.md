# jobwatch

Polls company job boards on Greenhouse, Lever and Ashby every 30 minutes and sends
newly posted roles that match your filters to Discord and/or Telegram. Runs free
on GitHub Actions.

- Filters by title (early-career SWE / frontend / full-stack), US location, and seniority.
- Drops roles requiring US citizenship or a security clearance (not possible on OPT).
- Keeps roles that say "no sponsorship" but flags them with ⚠️, so you can decide.
- A company you add is seeded silently on its first run: you're alerted only about jobs posted after that.

## Setup (about 10 minutes)

1. **Create a GitHub repo** (public is best: unlimited Actions minutes) and push these files.

2. **Pick a notification channel** and add it under *Settings → Secrets and variables → Actions → New repository secret*:
   - **Discord** (easiest): make a private server → channel settings → *Integrations → Webhooks → New Webhook → Copy URL*.
     Secret name: `DISCORD_WEBHOOK_URL`.
   - **Telegram**: message `@BotFather` → `/newbot` → copy the token (`TELEGRAM_BOT_TOKEN`).
     Send your bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id` (`TELEGRAM_CHAT_ID`).

3. **Check the company list** locally:
   ```bash
   pip install -r requirements.txt
   python jobwatch.py --check
   ```
   Fix or remove any `FAIL` lines in `config.yaml`. Companies switch ATS sometimes, so
   find the right slug by clicking a job on their careers page and reading the URL.

4. **Run it once** from the *Actions* tab → *jobwatch* → *Run workflow*. The first
   run only seeds `seen.json` (no alerts). After that it runs every 30 minutes.

## Tuning

Everything is in `config.yaml`:
- `companies`: add your targets, prioritizing ones with a record of sponsoring (MyVisaJobs / H1BGrader) and that use E-Verify.
- `title_include` / `title_exclude`: regexes on the job title.
- `location_include` / `location_exclude`: regexes on the location string.
- `description_exclude`: hard drop. `description_warn`: keep but flag.

Preview what would match without sending anything: `python jobwatch.py --dry-run`.

## Notes

- Big companies (Amazon, Google, Microsoft, Meta, Apple) run their own career sites,
  not these ATSes. Use the job alerts on their own sites for those.
- GitHub can delay scheduled runs by several minutes at busy times, and pauses
  schedules on repos with no activity for 60 days. If alerts stop, check the Actions tab.
- `seen.json` is committed back after each run; job IDs not seen for 60 days are pruned.
