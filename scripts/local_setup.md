# Local setup

How to set up LedgerMind on the Linux machine that runs it, start it, and check on it. Written
for a CentOS Stream 9 laptop (GNOME, `dnf`); on another distro, swap in its package manager.

The bot is one long-running process started by hand in `tmux`. There is no auto-restart: if the
machine reboots or the bot stops, start it again yourself. A missing 17:00 report is the sign
that it's down.

Everything the bot keeps lives inside the project folder: `config.toml`, `.env`, `data/` (the
database) and `logs/`.

## 1. Tools

Install git and tmux (needs your password):

```bash
sudo dnf install -y git tmux
```

Install uv for your user (no sudo). It also installs the right Python version later:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Open a new terminal afterwards so `uv` is on your `PATH`, then check:

```bash
uv --version
```

## 2. Clock and time zone

The bot works in UTC and pins the daily report to 17:00 Asia/Kolkata itself, so the machine's
time zone doesn't change when anything runs. Setting it to IST just makes `date` and the logs
easier to compare with the report. What matters is that the clock is right:

```bash
timedatectl
```

Look for `System clock synchronized: yes`. To set the time zone (optional):

```bash
sudo timedatectl set-timezone Asia/Kolkata
```

On a machine that dual-boots Windows, `timedatectl` may also say `RTC in local TZ: yes`. That's
Windows' way of keeping the hardware clock, and it's fine as long as the clock is synchronized.

## 3. Get the code

Clone it wherever you like (this guide uses `~/LedgerMind`). If the repository is private, sign
in with a GitHub token or SSH key first.

```bash
git clone https://github.com/AmethystMac/LedgerMind.git ~/LedgerMind
cd ~/LedgerMind
uv sync
```

`uv sync` downloads the Python version the project pins and installs the exact package
versions from `uv.lock`.

Create your settings. The bot refuses to start without `config.toml`:

```bash
cp config.example.toml config.toml
```

API keys go in `.env`, but only once a feature needs them (none do yet):

```bash
cp .env.example .env
```

Always run the bot's commands from the project folder: that's where it looks for
`config.toml`, `.env`, `data/` and `logs/`.

## 4. Keep the machine awake

A sleeping laptop is a stopped bot. Two things put it to sleep: closing the lid, and being idle
on battery. Change both once.

**Closing the lid.** Tell the system to ignore the lid switch, on battery and on mains power:

```bash
sudo mkdir -p /etc/systemd/logind.conf.d
printf '[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\n' | sudo tee /etc/systemd/logind.conf.d/ledgermind-lid.conf
```

Then reboot (restarting the login service instead can log you out of the desktop). After the
reboot, check it took effect:

```bash
systemd-analyze cat-config systemd/logind.conf | grep HandleLidSwitch
```

The last `HandleLidSwitch=` and `HandleLidSwitchExternalPower=` lines should say `ignore`.

**Idle on battery.** Run these in a terminal on the laptop's own desktop (not over SSH), because
they're GNOME settings for your user:

```bash
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-battery-type 'nothing'
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'
```

The screen still turns off and locks as usual; only suspend is disabled. With the lid closed,
keep the laptop somewhere it can breathe, not in a bag.

Check it once the bot is running (step 5): close the lid for five minutes, open it, and look at
the last heartbeats. There should be one per minute with no gap:

```bash
grep heartbeat logs/ledgermind.log | tail -n 8
```

## 5. Start the bot in tmux

tmux keeps the bot running after you close the terminal or SSH connection.

```bash
cd ~/LedgerMind
tmux new -s ledgermind
```

Inside tmux:

```bash
uv run python -m ledgermind run
```

You'll see a heartbeat line at start and then one every 60 seconds. To leave it running, detach
with **Ctrl+B, then D**. To come back later:

```bash
tmux attach -t ledgermind
```

To stop the bot cleanly, attach and press **Ctrl+C**. It finishes what it's doing, logs
"LedgerMind stopped cleanly" and exits. Type `exit` to close the tmux session.

## 6. Check on it

From any other terminal, or over SSH from another device:

```bash
ssh you@laptop-address
cd ~/LedgerMind
uv run python -m ledgermind status
```

`status` only reads the database, so it's safe while the bot runs. It shows the mode, the
database path, the last heartbeat and the last run of each job. A heartbeat from a few seconds
ago means the bot is alive. If `status` says the heartbeat is stale, the bot has stopped.

To find the laptop's address on your home network, run `hostname -I` on it. SSH needs the SSH
server running on the laptop (`sudo systemctl enable --now sshd`).

The full log is in `logs/ledgermind.log` (older ones are `ledgermind.log.1`, `.2`, ...):

```bash
tail -n 50 logs/ledgermind.log
```

## 7. Things that stop the bot

- **Rebooting or shutting down.** After any reboot, including one after `sudo dnf upgrade`,
  start the bot again (step 5). Before installing updates, stop the bot with Ctrl+C first.
- **Booting into Windows.** On a dual-boot laptop the bot only runs while Linux is up. Make sure
  Linux is the default boot entry, so an unplanned restart comes back into it.
- **Running out of battery.** The battery covers short power cuts; a long one shuts the laptop
  down. Keep it plugged in.
- **A VPN.** Turning a VPN on, or switching its server, changes the IP address the bot's
  requests come from. Paper mode doesn't care. Live mode will if the Binance API key is locked
  to one IP. Some sites also block or slow down VPN addresses, so if API calls start failing,
  try with the VPN off first.

## 8. Quick checklist when something looks wrong

1. `uv run python -m ledgermind status`: is the heartbeat recent?
2. `tmux ls`: is the `ledgermind` session still there? `tmux attach -t ledgermind` to see the
   last output.
3. `tail -n 100 logs/ledgermind.log`: look for `ERROR` or a traceback.
4. Restart: Ctrl+C in tmux (if it's still running), then `uv run python -m ledgermind run`.

Don't open the database with the system `sqlite3` tool: on CentOS Stream 9 it's too old for
this schema and reports `malformed database schema`. The database is fine; use `status`, or
Python through uv (`uv run python`), which brings its own newer SQLite.
