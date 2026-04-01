#!/usr/bin/env python3
"""
Synature recording sync script.

Downloads all recordings for a project and continuously polls for new ones.
State (last downloaded timestamp) is persisted to disk so the script can
resume safely after being killed.

Usage:
    python sync_recordings.py

Configuration:
    Edit the CONFIG block below, or set environment variables.

This script works with Python 3.10+
"""

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import httpx

CONFIG = {
    "base_url":    os.getenv("SYNATURE_URL",        "https://api.synature.ai"),
    "api_token":   os.getenv("SYNATURE_TOKEN",      "syn_your_token_here"),
    "project_id":  os.getenv("SYNATURE_PROJECT_ID", "your_project_id_here"),
    "storage_dir": os.getenv("SYNATURE_STORAGE_DIR","./data"),
    "state_file":  os.getenv("SYNATURE_STATE_FILE", "./sync_state.json"),
    "poll_interval_seconds": int(os.getenv("SYNATURE_POLL_INTERVAL", "60")),
    "page_size":   50,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("sync.log"),
    ],
)
log = logging.getLogger(__name__)

'''
State persistence
'''

def load_state(state_file: str) -> dict:
    """Load persisted sync state from disk."""
    path = Path(state_file)
    if path.exists():
        with open(path) as f:
            state = json.load(f)
        log.info("Resuming from state: last_recorded_at=%s, failed=%d", 
                 state.get("last_recorded_at"),
                 len(state.get("failed_ids", [])))
        return state
    log.info("No state file found - starting from the beginning")
    return {"last_recorded_at": None, "failed_ids": []}


def save_state(state_file: str, state: dict) -> None:
    """Atomically persist sync state to disk."""
    path = Path(state_file)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(path)  

'''
Utility functions
'''

def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def sanitize_filename(name: str) -> str:
    # Replace path separators so a filename like "one/two.flac"
    # doesn't create a subdirectory
    return name.replace("/", "_").replace("\\", "_")

'''
API interaction
'''
def make_client(config: dict) -> httpx.Client:
    return httpx.Client(
        base_url=config["base_url"],
        headers={"Authorization": f"Bearer {config['api_token']}"},
        timeout=30.0,
    )


def fetch_recordings_page(
    client: httpx.Client,
    config: dict,
    page: int,
    start_date: Optional[str],
) -> dict:
    """Fetch a single page of recordings, oldest first."""
    params = {
        "projectId": config["project_id"],
        "page":      page,
        "size":      config["page_size"],
    }
    if start_date:
        params["startDate"] = start_date

    response = client.get("/recordings/", params=params)
    response.raise_for_status()
    return response.json()


def iter_recordings(
    client: httpx.Client,
    config: dict,
    start_date: Optional[str],
):
    """
    Iterate over all recordings from oldest to newest.

    Yields one recording dict at a time, fetching pages as needed.
    Sorting oldest-first is critical: it means we can safely update
    last_recorded_at after each individual download without risk of
    skipping recordings if the script is killed mid-page.
    """
    page = 0
    total_pages = None

    while total_pages is None or page < total_pages:
        log.debug("Fetching page %d (start_date=%s)", page, start_date)
        data = fetch_recordings_page(client, config, page, start_date)

        if total_pages is None:
            total_pages = data["totalPages"]
            log.info(
                "Found %d recordings across %d pages",
                data["totalCount"],
                total_pages,
            )

        recordings = data["data"]

        # Sort oldest first within the page.
        # The API may already return them this way but we enforce it here
        # so that state updates are always monotonically increasing.
        recordings.sort(key=lambda r: r["recordedAt"])

        for recording in recordings:
            yield recording

        page += 1


def download_recording(
    recording: dict,
    storage_dir: Path,
) -> Optional[Path]:
    """
    Download a recording from its presigned audio_url.

    Returns the local path on success, None if already exists.
    The presigned URL is only valid for ~15 minutes so we must
    download immediately after fetching each page.
    """
    recorded_at = parse_iso(recording["recordedAt"])

    # Organise files by YYYY/MM/DD/filename for easy browsing
    date_path = storage_dir / recorded_at.strftime("%Y/%m/%d")
    date_path.mkdir(parents=True, exist_ok=True)

    raw_filename = recording.get("originalFilename") or f"{recording['id']}.flac"
    filename = sanitize_filename(raw_filename)  # ← fix #1
    dest = date_path / filename

    if dest.exists():
        log.debug("Already exists, skipping: %s", dest)
        return None

    audio_url = recording["audioUrl"]
    if not audio_url:
        log.warning("No audioUrl for recording %s, skipping", recording["id"])
        return None

    log.info("Downloading %s → %s", filename, dest)

    # Stream the download so we don't buffer the whole file in memory
    tmp = dest.with_suffix(".part")
    try:
        with httpx.stream("GET", audio_url, timeout=120.0) as response:
            response.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in response.iter_bytes(chunk_size=65536):
                    f.write(chunk)
        tmp.replace(dest)  # atomic: only appears if fully downloaded
    except Exception:
        tmp.unlink(missing_ok=True)  # clean up partial file on failure
        raise

    return dest


'''
Main sync logic
'''
def sync_once(client: httpx.Client, config: dict, state: dict) -> Tuple[int, int]:
    """
    Download all recordings newer than state["last_recorded_at"].

    Updates state after each successful download so progress is not
    lost if the script is killed. Returns the number of new files downloaded.
    """
    storage_dir = Path(config["storage_dir"])
    storage_dir.mkdir(parents=True, exist_ok=True)

    start_date = state.get("last_recorded_at")
    downloaded = 0
    failed_ids: set = set(state.get("failed_ids", []))
    failed = 0


    # First retry any previously failed recordings
    if failed_ids:
        log.info("Retrying %d previously failed recordings", len(failed_ids))
        still_failing = set()

        for recording_id in list(failed_ids):
            # Fetch the individual recording to get a fresh presigned URL
            try:
                response = client.get(
                    f"{config['base_url']}/recordings/{recording_id}",
                    timeout=30.0,
                )
                response.raise_for_status()
                recording = response.json()

                path = download_recording(recording, storage_dir)
                if path:
                    downloaded += 1
                log.info("Retry succeeded for %s", recording_id)

            except Exception as e:
                log.error("Retry failed for %s: %s", recording_id, e)
                still_failing.add(recording_id)
                failed += 1

        failed_ids = still_failing
        state["failed_ids"] = list(failed_ids)
        save_state(config["state_file"], state)

    for recording in iter_recordings(client, config, start_date):
        recording_id = recording["id"]

        try:
            path = download_recording(recording, storage_dir)
            if path:
                downloaded += 1

            # Update cursor only on success or skip
            state["last_recorded_at"] = recording["recordedAt"]
            state["failed_ids"] = list(failed_ids)
            save_state(config["state_file"], state)

        except Exception as e:
            log.error("Failed to download recording %s: %s", recording_id, e)
            # Record the failure but keep going
            failed_ids.add(recording_id)
            # Still advance the cursor so we don't re-fetch this recording
            # via the date filter on the next run - it's tracked in failed_ids instead
            state["last_recorded_at"] = recording["recordedAt"]
            state["failed_ids"] = list(failed_ids)
            save_state(config["state_file"], state)
            failed += 1
            continue

    return (downloaded, failed)


def run(config: dict) -> None:
    state = load_state(config["state_file"])

    with make_client(config) as client:
        # Initial sync, catch up on everything missed
        log.info("Starting initial sync")
        downloaded, failed = sync_once(client, config, state)
        log.info("Initial sync complete - downloaded %d recordings, %d failed", downloaded, failed)

        while True:
            interval = config["poll_interval_seconds"]
            log.info("Sleeping %ds before next poll", interval)
            time.sleep(interval)

            try:
                downloaded = sync_once(client, config, state)
                if downloaded:
                    log.info("Poll complete — downloaded %d new recordings", downloaded)
                else:
                    log.info("Poll complete — no new recordings")

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    log.error("API token rejected (401) — check your token and exit")
                    sys.exit(1)
                log.error("HTTP error during poll: %s", e)

            except httpx.RequestError as e:
                log.error("Network error during poll: %s — will retry", e)


if __name__ == "__main__":
    if "your_token_here" in CONFIG["api_token"]:
        print("ERROR: Set SYNATURE_TOKEN or edit CONFIG before running")
        sys.exit(1)
    run(CONFIG)
