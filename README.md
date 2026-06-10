# Zerocoda Appointment Alert

This monitors the Zerocoda API and alerts you when an appointment appears for:

- Service: `Scelta / Revoca / Cambio del medico`
- Search point: `45.45877,9.157912`
- Scope: every offered location returned by the booking search

It does not book a slot automatically. Booking would require the personal-data flow on Zerocoda and may involve CAPTCHA or confirmation steps, so this monitor focuses on fast notification without accidentally holding slots.

The monitor checks every offered service/facility pair returned by the booking search. At the time this was configured, that search returned these locations:

- `Stromboli - Ufficio Scelta e Revoca`
- `Bande Nere - Ufficio Scelta e Revoca`
- `Gola - Ufficio Scelta e Revoca`
- `Odazio - Ufficio Scelta e Revoca`
- `Monreale - Ufficio Scelta e Revoca`
- `Masaniello - Ufficio Scelta e Revoca`
- `Baroni - Ufficio Scelta e Revoca`

## Run Locally

Run one check:

```bash
python3 zc_monitor.py
```

Keep checking every 5 minutes and show a macOS notification when availability appears:

```bash
python3 zc_monitor.py --loop --interval 300
```

This only works while your Mac is awake and online.

The script exits with status `0` when an appointment is found and `1` when there is still no availability. For CI/scheduled jobs, use `--exit-zero-if-unavailable` so a normal "no slot yet" check does not show as failed.

## Telegram Alerts

Create a Telegram bot with `@BotFather`, send one message to the bot, then get your chat id:

```bash
curl "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates"
```

Look for `message.chat.id` in the response.

Test the Telegram alert locally:

```bash
export TELEGRAM_BOT_TOKEN="<YOUR_BOT_TOKEN>"
export TELEGRAM_CHAT_ID="<YOUR_CHAT_ID>"
python3 zc_monitor.py --test-telegram
```

Test the full appointment-alert message locally without waiting for a real slot:

```bash
python3 zc_monitor.py --simulate-available --notify --state-file .zc-monitor-simulation-state.json
```

To verify that Telegram is really configured, add `--require-telegram`; without the two Telegram environment variables it will fail:

```bash
python3 zc_monitor.py --simulate-available --notify --require-telegram --state-file .zc-monitor-simulation-state.json
```

## GitHub Actions

The workflow in `.github/workflows/zerocoda-monitor.yml` checks every 5 minutes on GitHub-hosted runners, so your Mac does not need to be on. The cron is offset from minute `0` to avoid the busiest GitHub Actions scheduling window.

Setup:

1. Push this folder to a GitHub repository.
2. In GitHub, go to `Settings` -> `Secrets and variables` -> `Actions`.
3. Add repository secrets:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. Open the `Actions` tab and enable workflows if GitHub asks you to.
5. Run `Zerocoda appointment monitor` manually once with `test_telegram` enabled. You should receive a basic Telegram test message.
6. Run it manually once with `simulate_available` enabled. You should receive a realistic appointment-style alert with the booking link.
7. Run it manually again with both options disabled, or wait for the next scheduled run.

Optional setup with the GitHub CLI:

```bash
gh repo create Alert-Tessera-apointment --private --source=. --remote=origin --push
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID
```

`gh secret set` will prompt you to paste each value without storing it in your shell history.

Before pushing, run the local preflight check:

```bash
./scripts/preflight.sh
```

Go-live checklist:

- The workflow file is committed and pushed to the repository's default branch.
- GitHub Actions are enabled for the repository.
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` exist under repository Actions secrets.
- You have run the workflow manually with `test_telegram` enabled and received the message on your phone.
- You have run the workflow manually with `simulate_available` enabled and received the appointment-style test alert.
- You have run the workflow manually with both options disabled and seen the real availability check complete.

How to know it is running:

- In GitHub, open `Actions` -> `Zerocoda appointment monitor`.
- A manual run with `test_telegram` enabled should show `Sent Telegram test message.` and your phone should receive it.
- A manual run with `simulate_available` enabled should send a message beginning with `TEST: Appointment available`.
- Each run should show a line like `No availability at any of 7 offered locations.` until a slot appears.
- When a slot appears, the workflow sends you a Telegram message with the booking link.
- Scheduled runs appear in the same workflow page. If you do not see scheduled runs after pushing to the default branch, check that Actions are enabled and that the workflow file is on that default branch.

The workflow keeps `.zc-monitor-state.json` in the GitHub Actions cache to avoid repeated Telegram alerts for the same available slot/location while availability remains unchanged. If a different offered location or a different slot becomes available, the monitor alerts again. It restores the newest `zerocoda-monitor-state-*` cache and saves a fresh cache only after a successful real availability check, so a temporary Zerocoda or Telegram failure will not mark an appointment as already alerted. Simulated alerts use a separate state file and do not affect the real monitor. GitHub schedules are not real-time and can occasionally be delayed, but this is much more reliable than a laptop that sleeps.

Booking URL:
<https://asst-santipaolocarlo.zerocoda.it/prenotazione/?zc_f=Scelta+%2F+Revoca+%2F+Cambio+del+medico&zc_p=45.45877%2C9.157912&zc_s=d>
