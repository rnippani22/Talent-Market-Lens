# Talent Tech Radar: live pipeline and shareable site

Every 6 hours a GitHub Action pulls the feeds in `sources.yml`, drops irrelevant or duplicate items,
categorises and summarises the rest, saves `site/data/items.json`, and redeploys the site.
Only headline, link, source, date and a short summary are stored; articles are never copied.

## Three classifier modes (picked automatically, no billing required)
| Mode | Needs | Quality |
|---|---|---|
| Claude | `ANTHROPIC_API_KEY` secret (paid Console credits) | Best summaries and categories |
| GitHub Models | Nothing extra: uses the workflow's built-in token and `models: read` | Good; free tier is rate limited |
| Built-in rules | Nothing | Rougher categories, short factual summaries, stricter filter, vendor tagging |

If a provider errors, the run drops to the next mode so the site keeps updating. Each item records how it
was classified in its `via` field, which helps when spot-checking.

## Set up (about 10 minutes)
1. Put these files at the **root** of a GitHub repo. The hidden `.github` folder is skipped by drag-and-drop uploads; create `.github/workflows/refresh.yml` with Add file > Create new file.
2. Settings > Pages > Source: **GitHub Actions**.
3. Settings > Actions > General > Workflow permissions: **Read and write**.
4. Actions tab > "Refresh and deploy" > Run workflow. The link appears in the run summary.
5. Optional: add `ANTHROPIC_API_KEY` under Settings > Secrets and variables > Actions for Claude-quality output.

## GitHub Models notes
- The workflow needs `models: read` under `permissions` (already in `refresh.yml`).
- Free-tier limits are set by GitHub and change. At the time of writing, low-tier models allow about 15 requests/minute, 150/day and 8,000 input / 2,000 output tokens per request. The script batches 10 items per request and pauses 5 seconds between calls, so a full run of 120 items uses about 12 requests; at 4 runs a day that stays under the daily cap.
- Change the model with the `GH_MODEL` env var (default `openai/gpt-4.1-mini`). If the model name is rejected, try `openai/gpt-4o-mini`.
- A 403 usually means Models isn't enabled for the account or org. The run then falls back to rules. Set `USE_GITHUB_MODELS=0` to skip this tier.

## Before relying on it
- Run `python ingest.py --check` locally (after `pip install -r requirements.txt`) to see which feeds respond. These URLs were not test-fetched.
- First run backfills `LOOKBACK_DAYS` (default 10).

## Limits
- GitHub Pages from a free public repo is public. For a team-only site, use Cloudflare Pages or Netlify with access controls, or a private repo on a plan that supports private Pages.
- Covers the news feed and AI categories. Analyst ratings, assessment dates and events are curated by hand.
- RSS gives headlines and short snippets, so summaries are limited. Classifiers can mislabel; spot-check weekly.

## Tuning
- Coverage: edit queries in `sources.yml`.
- Rules mode: edit `STRONG`, `NEG`, `VENDORS` and the rule lists near the top of `ingest.py`.
- Volume: lower `MAX_NEW_PER_RUN` or the cron frequency.
