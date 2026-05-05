# ArdenTrack Electron shell

Headless tray app: spawns the PyInstaller `ardentrack.exe`, handles `arden://` OAuth callbacks, and uses `electron-updater` for GitHub releases.

## Setup

1. Build the Windows daemon: produce `../dist/ardentrack.exe` (PyInstaller) or set `ARDENT_PYTHON_EXE` to a Python entrypoint.
2. Add `build/icon.ico` (tray). Without it, tray creation may fail on some systems.
3. `npm install` and set `SUPABASE_URL` / `SUPABASE_ANON_KEY` / `ARDEN_AS_API_SECRET` in the environment (or use a `.env` loader of your choice before `npm start`).

## Run

`npm start`

## NSIS / install path

Adjust `build` in `package.json` for your GitHub `owner`/`repo` and optional `nsis` `include` for install directory under `%LOCALAPPDATA%` if you customize the installer.
