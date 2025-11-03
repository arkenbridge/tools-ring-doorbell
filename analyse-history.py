import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from ring_doorbell import Auth, Ring
from ring_doorbell.exceptions import Requires2FAError, RingError

import getpass
import os
import pickle
import shutil
import urllib.error
import urllib.request

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

TOKEN_CACHE = os.path.expanduser("~/.ring_token.cache")
DEFAULT_HISTORY_LIMIT = 3000
WINDOW_END_HOUR = 5
WINDOW_END_MINUTE = 30
DOWNLOAD_DIR = Path("ring_videos")
HISTORY_PAGE_SIZE = 100  # number of events to request per API call
LOCAL_TZ_NAME = "Europe/London"  # Use None to fall back to system timezone
STATE_FILE = Path("ring_history_state.json")


def token_updater(token):
    with open(TOKEN_CACHE, "wb") as f:
        pickle.dump(token, f)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyse Ring doorbell history for early-morning events."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_HISTORY_LIMIT,
        help="Total number of history events to inspect per doorbell (default: %(default)s).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from the last saved checkpoint to continue further back in history.",
    )
    parser.add_argument(
        "--reset-resume",
        action="store_true",
        help="Clear any stored resume checkpoint before running.",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=STATE_FILE,
        help=f"Path to save resume state (default: {STATE_FILE}).",
    )
    parser.add_argument(
        "--download-dir",
        type=Path,
        default=DOWNLOAD_DIR,
        help=f"Directory to store downloaded videos (default: {DOWNLOAD_DIR}).",
    )
    return parser.parse_args()


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Warning: unable to read state file {path}: {exc}")
        return {}


def save_state(path: Path, state: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
    except OSError as exc:
        print(f"Warning: unable to write state file {path}: {exc}")


def load_auth():
    if os.path.exists(TOKEN_CACHE):
        with open(TOKEN_CACHE, "rb") as f:
            token = pickle.load(f)
        return Auth("MyRingApp/1.0", token, token_updater)
    # first-time login
    username = input("Ring email: ")
    password = getpass.getpass("Ring password: ")
    auth = Auth("MyRingApp/1.0", None, token_updater)
    try:
        auth.fetch_token(username, password)
    except Requires2FAError:
        code = input("2FA code: ")
        auth.fetch_token(username, password, code)
    return auth


def fetch_history(
    dev,
    total_limit: int,
    start_older_than: int | None = None,
) -> list[dict]:
    """Fetch up to total_limit history entries, paging as needed."""
    events: list[dict] = []
    older_than: int | None = start_older_than
    page = 0
    seen_ids: set[int] = set()

    while total_limit <= 0 or len(events) < total_limit:
        remaining = total_limit - len(events) if total_limit > 0 else HISTORY_PAGE_SIZE
        batch_limit = (
            HISTORY_PAGE_SIZE if total_limit <= 0 else min(remaining, HISTORY_PAGE_SIZE)
        )
        kwargs = {"limit": max(batch_limit, 1)}
        if older_than is not None:
            kwargs["older_than"] = older_than

        page += 1
        batch = list(dev.history(**kwargs))
        print(
            f"  • Page {page}: requested {kwargs['limit']}, received {len(batch)} event(s)"
        )

        if not batch:
            break

        for event in batch:
            event_id = event.get("id")
            if event_id is None:
                continue
            try:
                event_id_int = int(event_id)
            except (TypeError, ValueError):
                continue
            if event_id_int in seen_ids:
                continue
            seen_ids.add(event_id_int)
            events.append(event)
            if 0 < total_limit <= len(events):
                break

        older_than = batch[-1].get("id")
        if isinstance(older_than, str) and older_than.isdigit():
            older_than = int(older_than)
        if len(batch) < kwargs["limit"]:
            break
        if older_than is None:
            break

    return events


def main() -> None:
    args = parse_args()
    history_limit = max(args.limit, 1)
    download_dir = args.download_dir
    state_path: Path = args.state_file

    if args.reset_resume:
        try:
            state_path.unlink()
        except FileNotFoundError:
            pass

    state = load_state(state_path)
    doorbot_states: dict = state.setdefault("doorbots", {})

    if args.reset_resume:
        doorbot_states.clear()
    if args.resume and not doorbot_states:
        print("No resume state found; starting from the most recent events.")

    auth = load_auth()
    ring = Ring(auth)
    ring.update_data()

    # Adjust to your local timezone if needed
    if LOCAL_TZ_NAME and ZoneInfo:
        try:
            local_tz = ZoneInfo(LOCAL_TZ_NAME)
        except Exception as exc:  # pylint: disable=broad-except
            print(
                f"Warning: failed to load timezone '{LOCAL_TZ_NAME}' ({exc}); "
                "falling back to system timezone."
            )
            local_tz = datetime.now().astimezone().tzinfo
    else:
        local_tz = datetime.now().astimezone().tzinfo

    tz_label = getattr(local_tz, "key", str(local_tz))
    window_desc = (
        f"between 00:00 and {WINDOW_END_HOUR:02d}:{WINDOW_END_MINUTE:02d} "
        f"({tz_label})"
    )

    print(
        f"Filtering events {window_desc} and examining up to {history_limit} "
        "most recent events per doorbell."
    )

    hits = {}
    total_events = 0
    matching_events = 0
    doorbots = ring.devices()["doorbots"]
    download_dir.mkdir(parents=True, exist_ok=True)
    downloaded_files: list[Path] = []
    skipped_downloads = 0
    print(f"Located {len(doorbots)} doorbot(s).")
    oldest_event_ts: datetime | None = None
    state_dirty = False

    for dev in doorbots:
        print(f"- {dev.name}: fetching history...")
        device_key = str(dev.device_api_id)
        resume_id = None
        if args.resume:
            stored_resume = doorbot_states.get(device_key, {})
            resume_raw = stored_resume.get("older_than_id")
            if resume_raw:
                try:
                    resume_id = int(resume_raw)
                except (TypeError, ValueError):
                    print(
                        f"  invalid resume checkpoint {resume_raw!r}; "
                        "ignoring and starting from latest events."
                    )
                    resume_id = None
                else:
                    oldest_local = stored_resume.get("oldest_timestamp_local")
                    if oldest_local:
                        print(
                            f"  resuming from checkpoint older than id {resume_raw} "
                            f"(oldest local timestamp: {oldest_local})"
                        )
                    else:
                        print(f"  resuming from older_than id {resume_raw}")
            else:
                print("  no resume checkpoint found; starting from latest events.")

        history = fetch_history(dev, history_limit, start_older_than=resume_id)
        print(f"  collected {len(history)} event(s)")
        if not history:
            continue
        device_oldest_ts: datetime | None = None
        device_oldest_id: int | None = None
        for ev in history:
            total_events += 1
            created_at = ev.get("created_at")
            if isinstance(created_at, datetime):
                raw_ts = created_at
            elif isinstance(created_at, (int, float)):
                raw_ts = datetime.fromtimestamp(created_at, tz=timezone.utc)
            elif isinstance(created_at, str):
                try:
                    raw_ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                except ValueError:
                    print(
                        f"  • Skipping event {ev.get('id')} – unable to parse timestamp "
                        f"{created_at!r}"
                    )
                    continue
            else:
                print(
                    f"  • Skipping event {ev.get('id')} – unexpected timestamp type "
                    f"{type(created_at)}"
                )
                continue

            if raw_ts.tzinfo is None:
                raw_ts = raw_ts.replace(tzinfo=timezone.utc)

            if oldest_event_ts is None or raw_ts < oldest_event_ts:
                oldest_event_ts = raw_ts

            ts_utc = raw_ts.astimezone(timezone.utc)
            ts_local = ts_utc.astimezone(local_tz)
            hour = ts_local.hour
            minute = ts_local.minute
            event_id = ev.get("id")
            event_id_int = None
            if event_id is not None:
                try:
                    event_id_int = int(event_id)
                except (TypeError, ValueError):
                    event_id_int = None
                else:
                    if device_oldest_ts is None or raw_ts < device_oldest_ts:
                        device_oldest_ts = raw_ts
                        device_oldest_id = event_id_int

            if (hour < WINDOW_END_HOUR) or (
                hour == WINDOW_END_HOUR and minute <= WINDOW_END_MINUTE
            ):
                matching_events += 1
                day = ts_local.strftime("%Y-%m-%d")
                entry = {
                    "device": dev.name,
                    "kind": ev.get("kind"),
                    "time": ts_local.strftime("%H:%M:%S"),
                    "id": event_id,
                }
                hits.setdefault(day, []).append(entry)

                if event_id is None:
                    print("  • Skipping download – event has no id.")
                    continue

                timestamp_label = ts_local.strftime("%Y-%m-%d_%H-%M-%S")
                safe_device = "".join(
                    ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in dev.name
                ).strip("_")
                filename = f"{timestamp_label}"
                if safe_device:
                    filename += f"_{safe_device}"
                filename += ".mp4"
                target_path = download_dir / filename

                if target_path.exists():
                    print(f"  • Skipping download – already exists: {target_path}")
                    skipped_downloads += 1
                    continue

                ring_error: RingError | None = None
                try:
                    dev.recording_download(
                        event_id,
                        filename=str(target_path),
                    )
                except RingError as exc:
                    ring_error = exc

                if ring_error is None and target_path.exists():
                    print(f"  • Downloaded video to {target_path}")
                    downloaded_files.append(target_path)
                    continue

                if ring_error is not None:
                    print(
                        f"  • Primary download failed for {event_id}: {ring_error}"
                    )

                fallback_url = None
                try:
                    fallback_url = dev.recording_url(event_id)
                except RingError as url_exc:
                    print(
                        f"  • Unable to fetch recording URL for {event_id}: {url_exc}"
                    )

                if not fallback_url:
                    skipped_downloads += 1
                    continue

                try:
                    with urllib.request.urlopen(fallback_url) as response, open(
                        target_path, "wb"
                    ) as destination:
                        shutil.copyfileobj(response, destination)
                except (urllib.error.URLError, OSError) as url_err:
                    print(
                        f"  • Fallback download failed for {event_id}: {url_err}"
                    )
                    skipped_downloads += 1
                else:
                    print(f"  • Downloaded via fallback URL to {target_path}")
                    downloaded_files.append(target_path)

        if device_oldest_id is not None and device_oldest_ts is not None:
            doorbot_states[device_key] = {
                "older_than_id": str(device_oldest_id),
                "oldest_timestamp_utc": device_oldest_ts.astimezone(
                    timezone.utc
                ).isoformat(),
                "oldest_timestamp_local": device_oldest_ts.astimezone(
                    local_tz
                ).isoformat(),
                "last_run_utc": datetime.now(timezone.utc).isoformat(),
            }
            state_dirty = True

    if state_dirty:
        save_state(state_path, state)
        print(f"Updated resume state saved to {state_path}")

    print(
        f"\nTotal events checked: {total_events}. "
        f"Matching events in window: {matching_events}."
    )

    if oldest_event_ts:
        print(
            "Oldest event retrieved:",
            oldest_event_ts.astimezone(local_tz).strftime("%Y-%m-%d %H:%M:%S %Z"),
        )

    if hits:
        for day in sorted(hits.keys()):
            print(f"\n{day}")
            for row in hits[day]:
                print(
                    f"  {row['time']}  {row['kind']:<8}  {row['device']}  (id={row['id']})"
                )
    else:
        print(f"No events found {window_desc}.")

    print(f"Download attempts skipped or failed: {skipped_downloads}.")

    if downloaded_files:
        print("\nSaved recordings:")
        for path in downloaded_files:
            print(f"  {path}")
    elif matching_events:
        print(
            "\nNo recordings saved: device may lack subscription or files already existed."
        )


if __name__ == "__main__":
    main()
