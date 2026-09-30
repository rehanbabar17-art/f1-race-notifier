# F1 Race Notifier

A cron-friendly Formula 1 notification service. It sends race schedules for the next 30, 14, and 7 days, start-of-day reminders for sprint/qualifying/race sessions, and session results after practice, sprint, qualifying, and race sessions become available.

The script runs in GitHub Actions. **cron-job.org sends an authenticated workflow-dispatch request every five minutes**, so the repository does not need an always-on server.

GitHub Actions must be enabled and able to start a hosted runner for the repository. If a
workflow appears as failed before the first step, check the repository's **Actions** settings
and the account's GitHub Actions usage/billing status; this is a GitHub runner-account issue,
not an F1 API or ntfy configuration error.

## What it sends

- **Race schedules:** ntfy messages list Grand Prix races in the next 30, 14, and 7 days. A window is updated when a newly scheduled race first appears within it; each meeting is announced once per window.
- **Morning reminder:** one message on the user's local calendar day listing that day's Sprint, Qualifying, and Race sessions.
- **Results:** one message per Practice, Sprint, Qualifying, and Race session after OpenF1 publishes the final session result. If a result is not available yet, the next cron run retries it.
- **Deduplication:** sent-message state is stored in `f1-race-notifier/state.json` inside the shared Backblaze B2 bucket.

Results are intentionally limited to the top 10 entries in the notification to keep ntfy messages readable; the source API remains available for the complete result set.

## Data source

The notifier uses [OpenF1](https://openf1.org/), a public Formula 1 data API. It provides session schedules and session results for practice, qualifying, sprint, and race sessions. OpenF1 documents a free tier with no API key, historical data from 2023 onward, and a limit of 30 requests per minute. A five-minute cron interval stays comfortably below that limit for normal use.

OpenF1 is an independent, community-operated project and is not affiliated with Formula 1, FIA, or Formula One Management. Data can appear a few minutes after official results are published.

## Repository structure

```text
f1-race-notifier/
├── .github/workflows/f1-notifier.yml  # Workflow dispatched by cron-job.org
├── f1_notifier/main.py                # Schedule, reminders, results, and ntfy delivery
├── f1_notifier/b2_sync.py             # B2 state restore/upload helper
├── requirements.txt
└── README.md
```

## GitHub secrets

Create these repository secrets at **Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `NTFY_TOPIC` | The private ntfy topic shown below, or another topic you choose |
| `B2_KEY_ID` | Backblaze application key ID with access to the shared bucket |
| `B2_APPLICATION_KEY` | Backblaze application key |
| `B2_BUCKET` | `GithubRepoSecretRB17` |
| `B2_ENDPOINT` | `https://s3.us-east-005.backblazeb2.com` |

The B2 object is namespaced as `f1-race-notifier/state.json`. The application key should be restricted to the required bucket and must never be committed to Git.

### Configured ntfy topic

A randomized topic has been selected for this notifier:

```text
f1-race-alerts-7q4m9x2k8v6p
```

Subscribe to it in the ntfy app or visit:

```text
https://ntfy.sh/f1-race-alerts-7q4m9x2k8v6p
```

Keep this topic private. Anyone who knows a public ntfy topic can publish to it.

## cron-job.org configuration

Create a new cronjob at [cron-job.org](https://console.cron-job.org/).

### Schedule

Use a custom interval of **every 5 minutes**. This gives near-real-time result notifications while remaining below OpenF1's documented free-tier request limit.

### Request

- **Title:** `F1 Race Notifier`
- **URL:** `https://api.github.com/repos/rehanbabar17-art/f1-race-notifier/actions/workflows/f1-notifier.yml/dispatches`
- **Method:** `POST`
- **Timeout:** default is acceptable; GitHub usually returns the dispatch response quickly.

### Headers

```text
Authorization: Bearer YOUR_GITHUB_PAT
Accept: application/vnd.github+json
Content-Type: application/json
X-GitHub-Api-Version: 2022-11-28
```

The PAT used in the `Authorization` header needs permission to dispatch workflows in this repository. Do not put the ntfy topic or B2 application key in cron-job.org; they belong in GitHub Secrets.

### Body

Use raw JSON:

```json
{
  "ref": "main",
  "inputs": {
    "year": "2026",
    "timezone": "Asia/Karachi"
  }
}
```

For another season, change `year`. For morning reminders in another location, change `timezone` to a valid IANA timezone such as `Europe/London` or `America/New_York`.

### Test run

Use cron-job.org's **Test run** button. A successful request should return HTTP `204` from GitHub. Then open the repository's **Actions** tab and confirm the `F1 Race Notifier` run completes successfully.

## Manual GitHub test

Open **Actions → F1 Race Notifier → Run workflow**, choose the season and timezone, and run it. The workflow restores B2 state, checks OpenF1, sends any due messages, persists state, and removes the temporary local state file.

## Important timing behavior

Each race schedule is sent when the job first observes one or more unannounced meetings within its 30-, 14-, or 7-day window. If OpenF1 adds a race later, the next poll sends an updated full schedule for each applicable window. The morning message is sent on the first job run on that local calendar day; it is not scheduled at a particular local clock minute. Results are sent as soon as a five-minute poll observes that OpenF1 has published them.

If cron-job.org is disabled after repeated failures, inspect its execution history and GitHub Actions runs. cron-job.org documents automatic deactivation after more than 25 consecutive failures.

## License and data notice

This repository contains fan automation code. Formula 1, FIA, and related marks belong to their respective owners. OpenF1 is independent and intended for personal/non-commercial fan use.
