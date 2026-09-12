# Supreme Court Publication Alerts

This monitor checks the U.S. Supreme Court's **Opinions**, **Orders**, and
**Opinions relating to orders** pages every five minutes. It emails and texts
you when a new PDF link appears. It automatically follows the current Court
term and deduplicates alerts across runs.

## Recommended setup: GitHub Actions

1. Create a new GitHub repository and upload everything in this folder,
   including the hidden `.github` folder.
2. In the repository, open **Settings → Secrets and variables → Actions** and
   add the secrets below.
3. Open **Actions → Monitor Supreme Court postings → Run workflow** once.
   This first run records the existing postings without notifying you. Future
   runs alert only when the Court adds a new PDF.

### Email secrets

The monitor works with any SMTP provider. For Gmail, enable two-step
verification, create an App Password, and use:

| Secret | Gmail value |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `465` |
| `SMTP_USERNAME` | Your Gmail address |
| `SMTP_PASSWORD` | Your 16-character Gmail App Password |
| `EMAIL_FROM` | Your Gmail address |
| `EMAIL_TO` | The address that should receive alerts |

### Text-message secrets

Create a Twilio account and phone number, then add:

| Secret | Value |
|---|---|
| `TWILIO_ACCOUNT_SID` | Twilio Account SID |
| `TWILIO_AUTH_TOKEN` | Twilio Auth Token |
| `TWILIO_FROM` | Twilio number, including `+1` |
| `SMS_TO` | Your mobile number, including `+1` |

On a Twilio trial, the recipient number must be verified. Standard Twilio SMS
charges apply. If you omit either the complete email group or complete SMS
group, that channel is simply skipped.

## Local use

Python 3.10+ is the only dependency.

```bash
python scotus_monitor.py --dry-run
python scotus_monitor.py
python -m unittest discover -s tests
```

Set the same variables in your environment to enable notifications. Never put
passwords or tokens directly in the source or commit a `.env` file.

## Behavior and reliability

- A run fails without changing its state if any monitored page is unavailable.
- Alerts are marked as seen only after configured notification channels finish.
- The state file is committed by the workflow so it survives future runs.
- GitHub schedules can occasionally start a few minutes late; they are not
  guaranteed real-time jobs.
- The scraper identifies new PDF URLs, so harmless wording changes do not
  create duplicate alerts.

The tool is unofficial and is not affiliated with the Supreme Court of the
United States.
