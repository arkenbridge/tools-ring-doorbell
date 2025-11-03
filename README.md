# Ring Doorbell History Analyzer

This utility fetches Ring doorbell history in batches, identifies events during a quiet overnight window, and optionally downloads the associated recordings. It supports paging backwards through your history, resuming from the last processed point, and coping with daylight-saving changes.

## Prerequisites

- Python 3.10+
- `ring_doorbell` library (the script assumes it is already installed alongside its dependencies)
- Valid Ring credentials (email/password and 2FA if required)

## First-Time Authentication

The first run without an existing token cache prompts for your Ring email and password. If your account uses 2FA, you’ll be prompted for the verification code. Tokens are cached in `~/.ring_token.cache`; subsequent runs reuse that file until it expires.

## Default Behaviour

Running:

```
python analyse-history.py
```

will:

1. Fetch up to 3,000 recent history entries for each doorbell (`--limit` default).  
2. Convert all timestamps into the `Europe/London` timezone (handles daylight saving shifts).  
3. Report and download events between midnight and 05:30 (15 minute increments are configurable in the script).  
4. Save matching videos to `ring_videos/`, naming them `YYYY-MM-DD_HH-MM-SS_Device.mp4`.  
5. Persist a paging checkpoint in `ring_history_state.json`; this records the oldest event seen so you can resume later and continue backwards in time.

## Command-line Options

The script accepts these flags:

- `--limit N`  
  Number of history events to pull per run. Larger values mean longer runtime but fewer runs. Default is 3000.

- `--resume`  
  Resume from the last saved checkpoint (per doorbell). Use this to step backwards through history in batches.

- `--reset-resume`  
  Clear the stored checkpoint before fetching. Useful when you want to restart from the most recent events.

- `--state-file PATH`  
  Override the path of the resume state file. Defaults to `ring_history_state.json` in the current directory.

- `--download-dir DIR`  
  Override the directory where recordings are saved. Defaults to `ring_videos/`.

Example workflow to page through older events:

1. `python analyse-history.py --limit 3000 --reset-resume`  
   Fetches the latest 3,000 events and seeds a checkpoint.
2. `python analyse-history.py --limit 3000 --resume`  
   Fetches the next 3,000 older events, continuing from where the previous run stopped.
3. Repeat step 2 until you reach the desired historical depth.

## Output

The script prints:

- Which doorbells were found and how many events were collected.
- For each matching event, a summary with time, kind, device name, and ID.
- Count of total events inspected and matching events.
- Oldest event timestamp retrieved (immediately indicates how far back the batch went).
- Download status for each recording (including any fallback URL attempts).

If nothing falls into the overnight window, you’ll see a note stating that no events were found, and no recordings will be downloaded.

## Daylight Saving and Timezones

By default the script uses the `Europe/London` timezone via Python’s `zoneinfo`. Adjust the `LOCAL_TZ_NAME` constant in `analyse-history.py` if you live in a different region. If `zoneinfo` is unavailable or the zone fails to load, the script falls back to the system’s timezone.

## Token Cache and State Files

- Token cache: `~/.ring_token.cache`  
  Stores OAuth tokens received from Ring. Delete this file if you need to re-authenticate from scratch.

- Resume state: `ring_history_state.json`  
  Records the “older_than” checkpoint per doorbell. Delete or override via `--reset-resume` to start over from the newest data.

## Troubleshooting

- **404 when downloading recordings:** Older events may only be available via a signed share URL. The script automatically retries with `recording_url` as a fallback.
- **Slow runs:** Pulling thousands of events can take time due to API rate limits. Reduce `--limit` or take longer pauses between runs if Ring throttles you.
- **Timezone errors:** Ensure Python 3.9+ so `zoneinfo` is available; otherwise install `tzdata` or set `LOCAL_TZ_NAME = None` to rely solely on local system time.

## Safety

The script uses only documented Ring APIs via the `ring_doorbell` package. Keep the token cache private; it grants access to your Ring account data. Remove downloaded videos if you no longer need them.
