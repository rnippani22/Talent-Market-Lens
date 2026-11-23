# Talent Tech Radar: live pipeline and shareable site

Every 6 hours a GitHub Action pulls the feeds in `sources.yml`, drops irrelevant or duplicate items,
asks Claude to categorise and summarise the rest, saves `site/data/items.json`, and redeploys the site.
Only headline, link, source, date and a short model-written summary are stored; articles are never copied.

## Set up (about 10 minutes)
1. Create a new GitHub repository and push this folder to it.
2. Repo Settings > Secrets and variables > Actions > New secret: `ANTHROPIC_API_KEY`.
3. Repo Settings > Pages > Source: **GitHub Actions**.
4. Actions tab > "Refresh and deploy" > Run workflow. Your link appears in the run summary.
5. Share that link. Later runs happen automatically.

## Before relying on it
- Run `python ingest.py --check` locally (after `pip install -r requirements.txt`). The feed URLs were not test-fetched when this was written; remove any that fail.
- First run backfills `LOOKBACK_DAYS` (default 10). Set the env var higher for a deeper backfill.
- `ANTHROPIC_MODEL` defaults to `claude-sonnet-5-5`; set it to a cheaper model if cost matters.
- Without the API key the script falls back to keyword rules and no summaries, so set the key.

## Limits
- GitHub Pages sites from free public repos are public. For a team-only site, deploy the `site/` folder to Cloudflare Pages or Netlify behind their access controls, or use a private repo on a plan that supports private Pages.
- This covers the news feed and AI categories. Analyst ratings, assessment dates and events are curated by hand (see the Radar prototype) because analyst firms do not publish feeds.
- RSS only sees headlines and snippets, so summaries reflect that. The classifier can mislabel; spot-check weekly.

## Tuning
- Coverage: edit the queries in `sources.yml`.
- Relevance: edit the `RELEVANT` regex and the rules in `SYSTEM` in `ingest.py`.
- Cost: lower `MAX_NEW_PER_RUN` or the cron frequency in `.github/workflows/refresh.yml`.
