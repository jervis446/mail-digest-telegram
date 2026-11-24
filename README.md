# Mail Digest: your inboxes, sorted, on Telegram every morning

Once a day, read every configured inbox, sort the last 24 hours of mail into
**high / medium / low** priority, and send the high and medium lists to you on
Telegram (default) or WhatsApp. Runs on GitHub Actions. No paid services, no
third-party Python packages (standard library only).

| Piece | Choice | Cost |
|---|---|---|
| Scheduler | GitHub Actions cron (private repo) | Free (about 30 of the 2,000 free min/month) |
| Mail access | IMAP over SSL with a Google app password per account, read-only | Free |
| Sorting | Editable rules in `rules.json`; optional Gemini re-rank (off by default) | Free |
| Delivery (default) | Your own Telegram bot via the official Telegram Bot API | Free |
| Delivery (alternative) | [CallMeBot](https://www.callmebot.com/blog/free-api-whatsapp-messages/) WhatsApp API | Free, personal use, own number only. In Nov 2026 its page said the bot was full and not issuing new keys. |

The script opens INBOX read-only and fetches headers with `BODY.PEEK`, so nothing
is marked as read. It never reads message bodies.

What you get every morning in Telegram:

```
📬 Mail digest · Tue, 24 Nov
31 new across 3 inboxes
🔴 2 need action   🟡 9 worth a look   ⚪ 20 skipped

🔴 DO TODAY
1. ☐ Contract review needed by tomorrow
      Priya · work · 9:14 AM
2. ☐ Payment failed for your credit card bill
      Bank Alerts · personal · 7:02 AM

🟡 WORTH A LOOK
• Invoice INV-204 is due
   Razorpay · work · 8:40 AM
...
⚪ Skipped 20: updates 15, promos 3, forums 2
```

Subjects are tappable and open the mail in Gmail.

## Quick start (about 15 minutes)

1. Make a Telegram bot with @BotFather and press Start in it (step 1).
2. Make a Google app password for each Gmail you want included (step 2).
3. Click **Use this template** and create your own **private** copy (step 3).
4. In your copy, add secrets `TELEGRAM_BOT_TOKEN` and `IMAP_ACCOUNTS` (step 4).
5. Actions tab > **daily-mail-digest** > Run workflow > mode **test**. Then save the chat ID it prints as secret `TELEGRAM_CHAT_ID` (step 5).

Detailed steps below.

## Setup

Do these steps in order. You enter every password, code and secret yourself;
nothing here asks you to paste one into a chat.

### 1. Create your Telegram bot (on your phone)

1. Install Telegram and sign in.
2. Open <https://t.me/BotFather> (the official bot that creates bots) and tap Start.
3. Send `/newbot`. Give it any name, then a username that ends in `bot` (e.g. `myname_maildigest_bot`).
4. BotFather replies with a **token** like `123456789:AA...`. Treat it as a password. It goes in the `TELEGRAM_BOT_TOKEN` secret in step 4.
5. Tap the `t.me/<your bot>` link in that reply, then tap **Start** (or send `/start`). The bot can only message you after you do this.

You do not need to find your chat ID yourself. The first **test** run (step 5) detects it from your `/start` message and prints it in the log. Save it as the `TELEGRAM_CHAT_ID` secret so later runs do not depend on it.

<details>
<summary>WhatsApp instead (CallMeBot)</summary>

1. Open <https://www.callmebot.com/blog/free-api-whatsapp-messages/>. It shows the current bot phone number when slots are free.
2. Save that number in your contacts. In WhatsApp, send it: `I allow callmebot to send me messages`
3. The bot replies with your API key. Add secrets `CALLMEBOT_PHONE` (e.g. `+91XXXXXXXXXX`) and `CALLMEBOT_APIKEY`, and set the repository variable `DELIVERY` to `whatsapp`.
</details>

### 2. Create an app password for each mailbox

App passwords need 2-Step Verification on the account.

**Personal Gmail**
1. Turn on 2-Step Verification: <https://myaccount.google.com/signinoptions/twosv>
2. Create an app password: <https://myaccount.google.com/apppasswords> (name it `mail-digest`).
   Google shows a 16-character password once. Copy it without spaces.
3. IMAP is always on for Gmail personal accounts.

**Google Workspace (work or college Google account)**
1. As the Workspace admin, check in Admin console:
   - *Apps > Google Workspace > Gmail > End User Access*: **POP and IMAP access** is enabled.
   - *Security > Authentication > 2-step verification*: users are allowed to turn it on.
   - *Security > Access and data control > Less secure apps* no longer exists; app passwords are separate and still work when 2SV is on.
2. Sign in as the mailbox user, turn on 2-Step Verification, then create an app password at
   <https://myaccount.google.com/apppasswords>.
   If that page says the setting is not available, your admin policy blocks app passwords
   (for example when only security keys are allowed).

Revoke an app password at the same page any time; the digest for that account then reports an error line instead of failing silently.

### 3. Get your own copy of this repo

1. At the top of this repo page, click **Use this template > Create a new repository**.
2. Pick any name and choose **Private**. Click **Create repository**.

Keep it **private**. Mail subjects and senders show up in your Actions logs, and private
repos get 2,000 free Actions minutes per month on GitHub Free, far more than this job uses.
(Scheduled workflows in *public* repos are auto-disabled after 60 days without repo activity;
that rule does not apply to private repos.)

Never put passwords or tokens in the code. They go only in your copy's secrets (next step).

### 4. Add the GitHub secrets

In **your copy**: *Settings > Secrets and variables > Actions > New repository secret*, or with `gh`
(it prompts for the value, so it never lands in shell history):

```bash
gh secret set TELEGRAM_BOT_TOKEN
gh secret set IMAP_ACCOUNTS < accounts.json   # then delete accounts.json
```

| Secret | Required | Value |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes (Telegram) | The token BotFather gave you |
| `TELEGRAM_CHAT_ID` | after first test | Printed in the log of the first **test** run |
| `IMAP_ACCOUNTS` | yes | JSON list of mailboxes (below) |
| `CALLMEBOT_PHONE`, `CALLMEBOT_APIKEY` | WhatsApp only | See the WhatsApp section in step 1 |
| `VIP_SENDERS` | no | Comma-separated addresses that are always high priority |
| `VIP_DOMAINS` | no | Comma-separated domains that are always high priority |
| `GEMINI_API_KEY` | no | Only if you turn on LLM mode (see below) |

`IMAP_ACCOUNTS` format (`name` is the short label shown in the digest; `host` defaults to `imap.gmail.com`):

```json
[
  {"name": "personal", "user": "you@gmail.com",         "password": "16charapppassword"},
  {"name": "work",     "user": "you@yourcompany.com",   "password": "16charapppassword"}
]
```

Non-Google mailboxes work too: add `"host": "imap.example.com", "port": 993`.
Gmail category signals (Promotions, Social, Updates, Forums) are only available on Gmail/Workspace.

Optional repository **variables** (*Settings > Secrets and variables > Actions > Variables*):

| Variable | Default | Meaning |
|---|---|---|
| `DELIVERY` | auto | `telegram` or `whatsapp`; auto picks Telegram when `TELEGRAM_BOT_TOKEN` is set |
| `LOOKBACK_HOURS` | `24` | How far back to look |
| `MAX_MEDIUM` | `15` | Max medium items in the message |
| `LLM_MODE` | `off` | Set to `gemini` to enable the LLM re-rank |
| `GEMINI_MODEL` | none | Required when `LLM_MODE=gemini`; pick a current model id from <https://ai.google.dev/gemini-api/docs/models> |

### 5. Test

1. *Actions > daily-mail-digest > Run workflow*, mode **test**: you should get one Telegram message. Open the run log, find the line `Detected TELEGRAM_CHAT_ID=...` and save that number as secret `TELEGRAM_CHAT_ID`. If it says 0 private chats found, send `/start` to your bot again and rerun.
2. Run again with mode **dry-run**: the digest (with the reason for each priority) prints in the job log, nothing is sent.
3. Run with mode **send**: the real digest arrives on Telegram.

After that it runs daily at 08:00 IST (`30 2 * * *` UTC). To change the time, edit the `cron` line in `.github/workflows/digest.yml` (times are UTC; <https://crontab.guru> helps).
GitHub can start scheduled runs some minutes late.

## Run locally

```bash
python3 digest.py --sample --reasons
```

```bash
python3 -m unittest discover -s tests
```

Against real mailboxes without sending (put the variables in a local `.env`, which is git-ignored):

```bash
set -a; source .env; set +a; python3 digest.py --dry-run --reasons
```

## Tuning priorities

Edit `rules.json`:

- `high_keywords` (+45), `medium_keywords` (+20), `low_keywords` (-25) match whole words in the subject.
- VIP sender/domain +50, starred +30, Gmail "important" +10, personal (no bulk headers, Primary tab) +15.
- Promotions / Social / Forums -30, Updates -5, bulk mail (List-Unsubscribe or Precedence: bulk) -10, no-reply style senders -5.
- `mute_senders` / `mute_domains` push a sender to low.
- `thresholds`: score >= `high` is high, >= `medium` is medium, else low.

Run `python3 digest.py --dry-run --reasons` to see why each mail landed where it did.

## Optional LLM mode (privacy warning)

With `LLM_MODE=gemini`, the mails the rules scored as **medium** are sent to the Gemini API
for a second opinion. Only the subject (first 150 characters) and the sender's **domain** are
sent; never the full address or body. On any error the rule result stands.

**Warning:** Google's Gemini API terms for the *free* (unpaid) tier allow Google to use
submitted prompts and responses to improve its products, and human reviewers may read them.
Google advises not to send sensitive or personal information to unpaid services. Email subjects
can contain exactly that. Keep `LLM_MODE` off (the default, rules only) unless you accept this.
Check the current terms at <https://ai.google.dev/gemini-api/terms>.

## Limits and caveats

- Telegram messages use HTML formatting: a numbered "do today" checklist, tappable subjects that open the mail in Gmail, repeats collapsed (x2), and a one-line count of skipped low-priority mail. Split at about 3,800 characters (the limit is 4,096).
- CallMeBot is a free shared service run by one provider; delivery can lag and the service can change. Long WhatsApp digests are split into ~1,000-character parts.
- One digest per day; mail older than `LOOKBACK_HOURS` is ignored.
- Rules are heuristics. Expect to tune `rules.json` during the first week.

## License

MIT. Use, copy, modify, share or sell it however you like. See [LICENSE](LICENSE).
