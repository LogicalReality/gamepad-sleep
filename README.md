# gamepad-sleep

Automatically disconnect idle Bluetooth gamepads (such as the Sony DualSense) in **SteamOS Desktop Mode** (or any KDE Plasma systemd user session) without modifying read-only system files.

## Why?

In SteamOS Game Mode, Steam manages controller idle timeouts directly. In **Desktop Mode**, BlueZ and KDE Plasma do not disconnect idle Bluetooth HID gamepads automatically, leaving controllers like the DualSense powered on until their battery drains.

## How It Works

- **Zero external dependencies:** Uses only the Python standard library (`selectors`, `fcntl`, `struct`, `tomllib`).
- **Accurate device filtering:** Inspects `/sys/class/input/event*` to monitor only Bluetooth (`bustype == 0005`) gamepads (`BTN_JOYSTICK` / `BTN_GAMEPAD`), ignoring:
  - Bluetooth headphones/keyboards/mice (e.g. AVRCP media controls)
  - Controller motion sensors (`INPUT_PROP_ACCELEROMETER`) and touchpads
  - Built-in Steam Deck controls and Steam Input virtual controllers
- **Drift-resistant deadzone:** Queries `EVIOCGABS` per axis via `ioctl` (~5% span threshold) so stick micro-drift does not keep resetting the idle timer.
- **Desktop Mode only:** Runs as a `systemd --user` service bound to `plasma-workspace.target`, starting in Desktop Mode and stopping automatically when switching back to Game Mode.
- **Clean disconnect:** Calls `org.bluez.Device1.Disconnect` over D-Bus via `busctl` (with `bluetoothctl` fallback).

## Requirements

- Linux with BlueZ (`busctl` / `bluetoothctl`) and `systemd --user`
- Python 3.11+ (for built-in `tomllib` config parsing; works on Python 3.9+ with CLI flags/defaults)

## Installation

```bash
git clone https://github.com/LogicalReality/gamepad-sleep.git
cd gamepad-sleep
./install.sh
```

> **Note:** You can also double-click `install.sh` (or `update.sh` / `uninstall.sh`) in Dolphin to run it with visual progress and desktop notifications. If you downloaded a `.zip` instead of cloning with `git`, run `bash install.sh` once (it automatically sets executable permissions on all scripts).

All files are installed under your home directory (`~/.local/bin/`, `~/.config/systemd/user/`, `~/.config/gamepad-sleep/`), so SteamOS system updates will not wipe them. Re-running `./install.sh` safely restarts the service without creating duplicate processes.

## Configuration

Edit `~/.config/gamepad-sleep/config.toml`:

```toml
timeout_minutes = 15
scan_interval_seconds = 10
```

Then restart the service:

```bash
systemctl --user restart gamepad-sleep.service
```

You can also override settings on the command line for testing:

```bash
python3 ~/.local/bin/gamepad-sleep.py --timeout 15 --scan-interval 2 --dry-run --verbose
```

## Logs & Status

```bash
systemctl --user status gamepad-sleep.service
journalctl --user -u gamepad-sleep.service -f
```

## Updating

To update to the latest version and restart the service (works both with `git clone` and `.zip` downloads via the GitHub API):

```bash
./update.sh
```

To check for updates and show an interactive desktop notification (with **Update now** and **View changes on GitHub** buttons, without modifying anything automatically):

```bash
./update.sh --check
```

## Uninstall

```bash
./uninstall.sh
```
