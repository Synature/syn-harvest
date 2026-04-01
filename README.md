# SynHarvest

A lightweight Python script that continuously harvests audio recordings from a [SynApp](https://app.synature.ai) project to a local directory using the SynApp API.

---

## Features

- **Full backfill** - downloads all existing recordings on first run, oldest to newest
- **Incremental polling** - checks for new recordings at a configurable interval
- **Resumable** - persists a cursor to disk; pick up exactly where it left off after a restart or crash
- **Fault tolerant** - failed downloads are recorded and retried on the next run without blocking progress
- **Safe writes** - atomic file operations ensure no partial or corrupt files are left on disk

---

## Requirements

- Python 3.10+
- A Synature API token with at least `read` scope

---

## Installation
 
### 1. Install Python 3.10
 
If your system Python is older than 3.10, use [pyenv](https://github.com/pyenv/pyenv) to install and manage versions.
 
**macOS**
```bash
brew install pyenv
pyenv install 3.10
```
 
**Linux (Debian/Ubuntu)**
```bash
sudo apt install -y make build-essential libssl-dev zlib1g-dev \
  libbz2-dev libreadline-dev libsqlite3-dev curl \
  libncursesw5-dev xz-utils tk-dev libxml2-dev libxmlsec1-dev libffi-dev liblzma-dev
 
curl https://pyenv.run | bash
pyenv install 3.10
```
 
After installing pyenv, add the following to your `~/.bashrc` (or `~/.zshrc`) and then run `source ~/.bashrc`:
 
```bash
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)"
```
 
### 2. Clone the repository
 
```bash
git clone git@github.com:Synature/syn-harvest.git
cd syn-harvest
 
# Pin Python 3.10 for this directory
pyenv local 3.10
 
python3 --version  # should show 3.10.x
```
 
### 3. Create a virtual environment
 
```bash
python3 -m venv .venv
source .venv/bin/activate
```
 
Your prompt will show `(.venv)` when the environment is active. To deactivate later, run `deactivate`.
 
### 4. Install dependencies
 
```bash
pip install -r requirements.txt
```
 
---

## Configuration

The script is configured via environment variables. Create a `.env` file or export them in your shell:

| Variable | Required | Default | Description |
|---|---|---|---|
| `SYNATURE_TOKEN` | ✅ | - | API token (`syn_…`) |
| `SYNATURE_PROJECT_ID` | ✅ | - | ID of the project to sync |
| `SYNATURE_URL` | | `https://api.synature.ai` | Base API URL |
| `SYNATURE_STORAGE_DIR` | | `./data` | Directory to save audio files |
| `SYNATURE_STATE_FILE` | | `./sync_state.json` | Path for the persistent cursor file |
| `SYNATURE_POLL_INTERVAL` | | `60` | Seconds between polls after initial sync |

### Generating an API token

1. Log into SynApp
2. Go to **Profile → API Tokens**
3. Create a new token with the `read` scope
4. Copy the token - it is only shown once

---

## Usage

```bash
export SYNATURE_TOKEN="syn_your_token_here"
export SYNATURE_PROJECT_ID="your-project-id"
export SYNATURE_STORAGE_DIR="./data"

python src/harvest.py
```

On first run, the script will download all existing recordings. On subsequent runs or after being restarted, it resumes from where it left off:

```
2026-04-01 09:00:00  INFO      No state file found — starting from the beginning
2026-04-01 09:00:00  INFO      Starting initial sync
2026-04-01 09:00:01  INFO      Found 68 recordings across 2 pages
2026-04-01 09:00:01  INFO      Downloading Myotis_mystacinus_20240803_231804.flac → recordings/2024/08/03/
2026-04-01 09:00:02  INFO      Downloading Myotis_mystacinus_20240803_231805.flac → recordings/2024/08/03/
...
2026-04-01 09:01:14  INFO      Initial sync complete — downloaded 68 recordings
2026-04-01 09:01:14  INFO      Sleeping 300s before next poll
```

---

## File layout

Downloaded files are organised by recording date:

```
recordings/
└── 2024/
    └── 08/
        ├── 03/
        │   ├── Myotis_mystacinus_20240803_231804.flac
        │   └── Myotis_mystacinus_20240803_231805.flac
        └── 04/
            └── Pipistrellus_pipistrellus_20240804_003122.flac
```

---

## State file

The script writes a `sync_state.json` file to track its progress:

```json
{
  "last_recorded_at": "2024-08-04T00:31:22Z",
  "failed_ids": []
}
```

- **`last_recorded_at`** - timestamp of the last successfully downloaded recording. Used as `startDate` on subsequent runs so only newer recordings are fetched.
- **`failed_ids`** - list of recording UUIDs that failed to download. Retried at the start of every sync run with a fresh presigned URL. Remove an entry manually to permanently skip a recording.

Delete the state file to trigger a full re-sync from the beginning.

---

## Running as a service

To run the script continuously in the background on a Linux server, create a systemd unit:

**`/etc/systemd/system/syn-harvest.service`**
```ini
[Unit]
Description=SynHarvest recording sync
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/opt/syn-harvest
Environment="SYNATURE_TOKEN=syn_your_token_here"
Environment="SYNATURE_PROJECT_ID=your-project-id"
Environment="SYNATURE_STORAGE_DIR=/data/recordings"
ExecStart=/opt/synharvest/.venv/bin/python harvest.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now syn-harvest
sudo journalctl -u syn-harvest -f   # follow logs
```

---

## Notes

- Audio URLs returned by the API are presigned S3 URLs valid for approximately 15 minutes. The script downloads each file immediately after fetching its page to avoid expiry.
- The script exits immediately with a non-zero status if the API returns `401`. Check that your token has not been revoked and has `read` scope.
- All downloads and state writes use a `.part` / `.tmp` staging file and an atomic rename - a crash or power loss will never leave a corrupt file that looks complete.
