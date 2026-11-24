#!/usr/bin/env python3
"""Daily email priority digest -> Telegram (Bot API) or WhatsApp (CallMeBot).

Standard library only. Reads mail over IMAP (read-only, does not mark messages
as read), scores each message into high / medium / low with editable rules,
optionally asks Gemini to re-rank the ambiguous ones, and sends the high and
medium lists to you on Telegram (your own bot) or WhatsApp (CallMeBot).

Delivery: DELIVERY=telegram|whatsapp. If unset, Telegram is used when
TELEGRAM_BOT_TOKEN is set, else WhatsApp.

Usage:
  python digest.py --sample            # fake data, prints digest, no network
  python digest.py --dry-run           # real mailboxes, prints digest, no send
  python digest.py                     # real mailboxes, sends the digest
  python digest.py --test-message      # sends one test message only
"""

from __future__ import annotations

import argparse
import datetime as dt
import email
import email.header
import email.utils
import html as htmllib
import imaplib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
RULES_PATH = HERE / "rules.json"
CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/{method}"
TELEGRAM_CHUNK_CHARS = 3800  # Telegram caps a message at 4096 characters
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Conservative chunk size per WhatsApp message (CallMeBot sends text via GET).
CHUNK_CHARS = 1000


@dataclass
class Mail:
    account: str
    sender_name: str
    sender_addr: str
    subject: str
    date: dt.datetime | None
    flags: set[str] = field(default_factory=set)  # e.g. starred, important, promotions
    bulk: bool = False  # List-Unsubscribe / Precedence: bulk
    score: int = 0
    priority: str = "medium"
    reasons: list[str] = field(default_factory=list)
    link: str = ""  # Gmail web link to the message, when available


# ---------------------------------------------------------------- config

def env(name: str, default: str | None = None) -> str | None:
    val = (os.environ.get(name) or "").strip()
    return val if val not in (None, "") else default


def load_rules() -> dict:
    rules = json.loads(RULES_PATH.read_text())
    # VIP senders / domains live in env (secrets) so they are not committed.
    for key, env_name in (("vip_senders", "VIP_SENDERS"), ("vip_domains", "VIP_DOMAINS")):
        extra = env(env_name, "")
        rules[key] = [s.strip().lower() for s in rules.get(key, []) + extra.split(",") if s.strip()]
    return rules


def load_accounts() -> list[dict]:
    raw = env("IMAP_ACCOUNTS")
    if not raw:
        sys.exit("IMAP_ACCOUNTS is not set. See README.")
    try:
        # strict=False tolerates line breaks / tabs pasted inside quoted values.
        accounts = json.loads(raw, strict=False)
    except json.JSONDecodeError as exc:
        sys.exit(f"IMAP_ACCOUNTS is not valid JSON ({exc.msg} at char {exc.pos}).")
    if isinstance(accounts, dict):
        accounts = [accounts]
    for i, acc in enumerate(accounts):
        for k in ("user", "password"):
            if not acc.get(k):
                sys.exit(f"IMAP_ACCOUNTS[{i}] is missing '{k}'.")
        acc["user"] = str(acc["user"]).strip()
        # Google app passwords are 16 letters, often shown in groups with spaces.
        acc["password"] = re.sub(r"\s+", "", str(acc["password"]))
        acc.setdefault("host", "imap.gmail.com")
        acc.setdefault("name", acc["user"].split("@")[-1])
    return accounts


# ---------------------------------------------------------------- IMAP

def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = []
    for text, enc in email.header.decode_header(value):
        if isinstance(text, bytes):
            parts.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            parts.append(text)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _gm_search(conn: imaplib.IMAP4_SSL, query: str) -> set[bytes]:
    typ, data = conn.uid("SEARCH", "X-GM-RAW", f'"{query}"')
    if typ != "OK" or not data or not data[0]:
        return set()
    return set(data[0].split())


def fetch_account(acc: dict, hours: int) -> list[Mail]:
    conn = imaplib.IMAP4_SSL(acc["host"], int(acc.get("port", 993)))
    try:
        conn.login(acc["user"], acc["password"])
        conn.select("INBOX", readonly=True)
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
        is_gmail = "X-GM-EXT-1" in conn.capabilities

        days = max(1, (hours + 23) // 24)
        categories: dict[str, set[bytes]] = {}
        if is_gmail:
            uids = _gm_search(conn, f"in:inbox newer_than:{days}d")
            for cat in ("promotions", "social", "updates", "forums"):
                categories[cat] = _gm_search(conn, f"in:inbox newer_than:{days}d category:{cat}")
        else:
            since = (cutoff - dt.timedelta(days=1)).strftime("%d-%b-%Y")
            typ, data = conn.uid("SEARCH", None, "SINCE", since)
            uids = set(data[0].split()) if typ == "OK" and data and data[0] else set()

        mails: list[Mail] = []
        if not uids:
            return mails
        uid_list = b",".join(sorted(uids, key=int))
        items = "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID LIST-UNSUBSCRIBE PRECEDENCE)]"
        items += " X-GM-LABELS)" if is_gmail else ")"
        typ, data = conn.uid("FETCH", uid_list, items)
        if typ != "OK":
            return mails
        for part in data:
            if not isinstance(part, tuple):
                continue
            meta, header_bytes = part
            uid_m = re.search(rb"UID (\d+)", meta)
            uid = uid_m.group(1) if uid_m else b""
            msg = email.message_from_bytes(header_bytes)
            date = None
            try:
                date = email.utils.parsedate_to_datetime(msg.get("Date"))
                if date and date.tzinfo is None:
                    date = date.replace(tzinfo=dt.timezone.utc)
            except (TypeError, ValueError):
                pass
            if date and date < cutoff:
                continue
            name, addr = email.utils.parseaddr(_decode(msg.get("From")))
            flags: set[str] = set()
            meta_s = meta.decode(errors="replace")
            if "\\Flagged" in meta_s or "\\\\Starred" in meta_s:
                flags.add("starred")
            if "\\\\Important" in meta_s or "\\Important" in meta_s:
                flags.add("important")
            for cat, cat_uids in categories.items():
                if uid in cat_uids:
                    flags.add(cat)
            bulk = bool(msg.get("List-Unsubscribe")) or \
                (msg.get("Precedence", "").lower() in ("bulk", "list", "junk"))
            link = ""
            mid = (msg.get("Message-ID") or "").strip().strip("<>")
            if is_gmail and mid:
                link = (
                    "https://mail.google.com/mail/u/?authuser="
                    + urllib.parse.quote(acc["user"])
                    + "#search/" + urllib.parse.quote(f"rfc822msgid:{mid}", safe="")
                )
            mails.append(Mail(
                account=acc["name"], sender_name=name or addr, sender_addr=addr.lower(),
                subject=_decode(msg.get("Subject")) or "(no subject)", date=date,
                flags=flags, bulk=bulk, link=link,
            ))
        return mails
    finally:
        try:
            conn.logout()
        except Exception:
            pass


# ---------------------------------------------------------------- scoring

def _has_any(text: str, words: list[str]) -> str | None:
    for w in words:
        if re.search(r"(?<![a-z0-9])" + re.escape(w.lower()) + r"(?![a-z0-9])", text):
            return w
    return None


def classify(m: Mail, rules: dict) -> None:
    subj = m.subject.lower()
    domain = m.sender_addr.split("@")[-1]
    s, why = 0, []

    if m.sender_addr in rules["vip_senders"] or domain in rules["vip_domains"]:
        s += 50; why.append("VIP")
    if m.sender_addr in [x.lower() for x in rules.get("mute_senders", [])] or \
            domain in [x.lower() for x in rules.get("mute_domains", [])]:
        s -= 100; why.append("muted")
    if "starred" in m.flags:
        s += 30; why.append("starred")
    if "important" in m.flags:
        s += 10; why.append("gmail-important")
    if (w := _has_any(subj, rules["high_keywords"])):
        s += 45; why.append(f"kw:{w}")
    if (w := _has_any(subj, rules["medium_keywords"])):
        s += 20; why.append(f"kw:{w}")
    if (w := _has_any(subj, rules["low_keywords"])):
        s -= 25; why.append(f"low-kw:{w}")
    for cat in ("promotions", "social", "forums"):
        if cat in m.flags:
            s -= 30; why.append(cat)
    if "updates" in m.flags:
        s -= 5; why.append("updates")
    if m.bulk:
        s -= 10; why.append("bulk")
    if re.match(r"^(no-?reply|do-?not-?reply|notifications?|newsletter|marketing|info|news)@", m.sender_addr):
        s -= 5; why.append("automated")
    if not m.bulk and not m.flags.intersection({"promotions", "social", "forums", "updates"}):
        s += 15; why.append("personal")

    m.score, m.reasons = s, why
    t = rules["thresholds"]
    m.priority = "high" if s >= t["high"] else "medium" if s >= t["medium"] else "low"


# ---------------------------------------------------------------- optional LLM

def gemini_rerank(mails: list[Mail], rules: dict) -> None:
    """Re-rank only the borderline (medium) mails. Sends subject + sender domain only."""
    key, model = env("GEMINI_API_KEY"), env("GEMINI_MODEL")
    if not key or not model:
        print("LLM_MODE=gemini but GEMINI_API_KEY / GEMINI_MODEL not set; using rules only.", file=sys.stderr)
        return
    pool = [m for m in mails if m.priority == "medium"][:60]
    if not pool:
        return
    lines = [f"{i}. from-domain={m.sender_addr.split('@')[-1]} | subject={m.subject[:150]}"
             for i, m in enumerate(pool)]
    prompt = (
        "Classify each email for a busy founder's daily digest as high, medium or low priority. "
        "high = needs action today (money, legal, security, deadlines, direct personal requests). "
        "low = newsletters, marketing, routine notifications. "
        'Reply with JSON only: {"items":[{"i":0,"p":"high"}, ...]}\n\n' + "\n".join(lines)
    )
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
    }).encode()
    req = urllib.request.Request(
        GEMINI_URL.format(model=urllib.parse.quote(model)), data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.load(r)
        text = out["candidates"][0]["content"]["parts"][0]["text"]
        for it in json.loads(text).get("items", []):
            i, p = int(it["i"]), str(it["p"]).lower()
            if 0 <= i < len(pool) and p in ("high", "medium", "low"):
                if p != pool[i].priority:
                    pool[i].reasons.append(f"llm:{p}")
                pool[i].priority = p
    except Exception as exc:  # fail open: keep rule results
        print(f"Gemini rerank skipped: {type(exc).__name__}", file=sys.stderr)


# ---------------------------------------------------------------- output

def _group(mails: list[Mail]) -> list[tuple[Mail, int]]:
    """Collapse repeats (same account, sender, subject) into one row with a count."""
    groups: dict[tuple, list[Mail]] = {}
    for m in mails:
        groups.setdefault((m.account, m.sender_name.lower(), m.subject.lower()), []).append(m)
    rows = []
    for ms in groups.values():
        newest = max(ms, key=lambda x: x.date or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
        best = max(ms, key=lambda x: x.score)
        best.date, best.link = newest.date, newest.link or best.link
        rows.append((best, len(ms)))
    return sorted(rows, key=lambda r: (-r[0].score, -(r[0].date.timestamp() if r[0].date else 0)))


def _low_breakdown(low: list[Mail]) -> str:
    buckets: dict[str, int] = {}
    for m in low:
        if "promotions" in m.flags:
            k = "promos"
        elif "social" in m.flags:
            k = "social"
        elif "forums" in m.flags:
            k = "forums"
        elif "updates" in m.flags:
            k = "updates"
        elif m.bulk:
            k = "newsletters"
        else:
            k = "other"
        buckets[k] = buckets.get(k, 0) + 1
    return ", ".join(f"{k} {v}" for k, v in sorted(buckets.items(), key=lambda kv: -kv[1]))


def build_digest(mails: list[Mail], errors: list[str], show_reasons: bool = False,
                 html: bool = False) -> str:
    """Render the digest. html=True gives Telegram HTML; otherwise WhatsApp-style text."""
    tz = dt.timezone(dt.timedelta(minutes=int(env("TZ_OFFSET_MINUTES", "330"))))
    today = dt.datetime.now(tz).strftime("%a, %d %b")
    max_med = int(env("MAX_MEDIUM", "15"))
    high = _group([m for m in mails if m.priority == "high"])
    med = _group([m for m in mails if m.priority == "medium"])
    low = [m for m in mails if m.priority == "low"]
    multi_acct = len({m.account for m in mails}) > 1

    esc = (lambda s: htmllib.escape(s, quote=False)) if html else (lambda s: s)

    def b(s: str) -> str:
        return f"<b>{s}</b>" if html else f"*{s}*"

    def it(s: str) -> str:
        return f"<i>{s}</i>" if html else f"_{s}_"

    def link(text: str, url: str) -> str:
        return f'<a href="{htmllib.escape(url)}">{text}</a>' if html and url else text

    def when(m: Mail) -> str:
        if not m.date:
            return ""
        local = m.date.astimezone(tz)
        same_day = local.date() == dt.datetime.now(tz).date()
        return local.strftime("%-I:%M %p") if same_day else local.strftime("%a %-I:%M %p")

    def meta(m: Mail, count: int) -> str:
        bits = [esc(m.sender_name[:32])]
        if multi_acct:
            bits.append(esc(m.account))
        if (w := when(m)):
            bits.append(w)
        if count > 1:
            bits.append(f"x{count}")
        if show_reasons:
            bits.append(esc(", ".join(m.reasons)))
        return " · ".join(bits)

    total = len(mails)
    accounts = len({m.account for m in mails})
    out = [
        f"📬 {b('Mail digest')} · {today}",
        it(f"{total} new across {accounts} inbox{'es' if accounts != 1 else ''}"),
        f"🔴 {len(high)} need action   🟡 {len(med)} worth a look   ⚪ {len(low)} skipped",
        "",
        f"🔴 {b('DO TODAY')}",
    ]
    if high:
        for n, (m, c) in enumerate(high, 1):
            out.append(f"{n}. ☐ {b(link(esc(m.subject[:110]), m.link))}")
            out.append(f"      {it(meta(m, c))}")
    else:
        out.append(it("Nothing urgent. Enjoy your day."))

    out += ["", f"🟡 {b('WORTH A LOOK')}"]
    if med:
        for m, c in med[:max_med]:
            out.append(f"• {link(esc(m.subject[:90]), m.link)}")
            out.append(f"   {it(meta(m, c))}")
        if len(med) > max_med:
            out.append(it(f"+ {len(med) - max_med} more"))
    else:
        out.append(it("Nothing here."))

    if low:
        out += ["", f"⚪ {it(f'Skipped {len(low)}: {_low_breakdown(low)}')}"]
    if errors:
        out += ["", f"⚠️ {b('Could not read')}"] + [f"• {esc(e)}" for e in errors]
    return "\n".join(out)


def chunk(text: str, size: int = CHUNK_CHARS) -> list[str]:
    chunks, cur = [], ""
    for ln in text.split("\n"):
        ln = ln[:size]
        if len(cur) + len(ln) + 1 > size:
            chunks.append(cur.rstrip("\n")); cur = ""
        cur += ln + "\n"
    if cur.strip():
        chunks.append(cur.rstrip("\n"))
    return chunks


def send_whatsapp(text: str) -> None:
    phone, key = env("CALLMEBOT_PHONE"), env("CALLMEBOT_APIKEY")
    if not phone or not key:
        sys.exit("CALLMEBOT_PHONE / CALLMEBOT_APIKEY not set. See README.")
    parts = chunk(text)
    for n, part in enumerate(parts, 1):
        if len(parts) > 1:
            part = f"({n}/{len(parts)})\n{part}"
        qs = urllib.parse.urlencode({"phone": phone, "text": part, "apikey": key})
        with urllib.request.urlopen(f"{CALLMEBOT_URL}?{qs}", timeout=60) as r:
            body = r.read().decode(errors="replace")
        # CallMeBot answers HTTP 200 with an HTML page; treat known error words as failure.
        low = body.lower()
        if r.status != 200 or "apikey is invalid" in low or ("error" in low and "queued" not in low):
            sys.exit(f"CallMeBot send failed (HTTP {r.status}). Response start: {re.sub('<[^>]+>', ' ', body)[:200]!r}")
        print(f"Sent WhatsApp part {n}/{len(parts)}")
        if n < len(parts):
            time.sleep(8)


def _telegram(token: str, method: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        TELEGRAM_URL.format(token=token, method=method), data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:  # Telegram returns JSON errors with 4xx
        try:
            body = json.load(exc)
        except Exception:
            body = {}
        desc = body.get("description", f"HTTP {exc.code}")
        sys.exit(f"Telegram {method} failed: {desc}")
    except urllib.error.URLError as exc:
        # The URL embeds the token; report only the reason.
        sys.exit(f"Telegram {method} failed: {exc.reason}")


def telegram_chat_id(token: str) -> str:
    chat_id = env("TELEGRAM_CHAT_ID")
    if chat_id:
        return chat_id
    # Not configured yet: find the private chat of whoever messaged the bot.
    res = _telegram(token, "getUpdates")
    chats = {}
    for upd in res.get("result", []):
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat", {})
        if chat.get("type") == "private":
            chats[str(chat["id"])] = chat.get("first_name", "")
    if len(chats) != 1:
        sys.exit(
            "TELEGRAM_CHAT_ID is not set and it could not be detected "
            f"({len(chats)} private chats found). Open your bot in Telegram, send it /start, "
            "and run again within 24 hours."
        )
    chat_id = next(iter(chats))
    print(f"Detected TELEGRAM_CHAT_ID={chat_id}. Save it as a repo secret so later runs do not depend on it.")
    return chat_id


def send_telegram(text: str, html: bool = False) -> None:
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN not set. See README.")
    if not re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,}", token):
        # Describe the shape only; never print the value.
        left, _, right = token.partition(":")
        sys.exit(
            "TELEGRAM_BOT_TOKEN does not look like a bot token "
            f"(length {len(token)}, colon {'yes' if ':' in token else 'no'}, "
            f"digits before colon {len(left) if left.isdigit() else 'no'}, "
            f"chars after colon {len(right)}, "
            f"non-ASCII {'yes' if any(ord(c) > 127 for c in token) else 'no'}, "
            f"spaces inside {'yes' if ' ' in token else 'no'}). "
            "Copy only the line like 123456789:AA... from BotFather."
        )
    chat_id = telegram_chat_id(token)
    # Each digest line closes its own HTML tags, so line-based chunks stay valid.
    parts = chunk(text, TELEGRAM_CHUNK_CHARS)
    for n, part in enumerate(parts, 1):
        payload = {"chat_id": chat_id, "text": part, "link_preview_options": {"is_disabled": True}}
        if html:
            payload["parse_mode"] = "HTML"
        res = _telegram(token, "sendMessage", payload)
        if not res.get("ok"):
            sys.exit(f"Telegram sendMessage failed: {res.get('description', 'unknown error')}")
        print(f"Sent Telegram part {n}/{len(parts)}")


def delivery_mode() -> str:
    mode = (env("DELIVERY") or ("telegram" if env("TELEGRAM_BOT_TOKEN") else "whatsapp")).lower()
    if mode not in ("telegram", "whatsapp"):
        sys.exit(f"Unknown DELIVERY={mode!r}; use telegram or whatsapp.")
    return mode


def deliver(text: str, html: bool = False) -> None:
    if delivery_mode() == "telegram":
        send_telegram(text, html=html)
    else:
        send_whatsapp(text)


# ---------------------------------------------------------------- sample data

def sample_mails() -> list[Mail]:
    now = dt.datetime.now(dt.timezone.utc)
    rows = [
        ("personal", "Bank Alerts", "alerts@hdfcbank.example", "Payment failed for your credit card bill", set(), True),
        ("work", "Priya (Client)", "priya@client.example", "Contract review needed by tomorrow", {"important"}, False),
        ("work", "GitHub", "noreply@github.com", "[repo] CI failed on main", {"updates"}, True),
        ("personal", "Amazon", "store-news@amazon.example", "Big sale: 70% off today only", {"promotions"}, True),
        ("personal", "LinkedIn", "messages-noreply@linkedin.com", "You appeared in 9 searches", {"social"}, True),
        ("work", "Google Workspace", "workspace-noreply@google.com", "Security alert: new sign-in", {"updates"}, True),
        ("personal", "Rahul", "rahul@gmail.example", "Dinner Saturday?", set(), False),
        ("work", "Razorpay", "no-reply@razorpay.example", "Invoice INV-204 is due", {"updates"}, True),
        ("personal", "Medium Daily Digest", "noreply@medium.com", "Top stories for you", {"updates"}, True),
    ]
    return [Mail(a, n, e, s, now, f, b) for a, n, e, s, f, b in rows]


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the digest, do not send")
    ap.add_argument("--sample", action="store_true", help="use built-in fake emails (implies --dry-run)")
    ap.add_argument("--reasons", action="store_true", help="show why each mail got its priority")
    ap.add_argument("--html", action="store_true", help="dry-run: print the Telegram HTML version")
    ap.add_argument("--test-message", action="store_true", help="send one test message and exit")
    args = ap.parse_args()

    if args.test_message:
        deliver("Test from email-whatsapp-digest: delivery works.")
        return 0

    rules = load_rules()
    errors: list[str] = []
    if args.sample:
        mails = sample_mails()
        args.dry_run = True
    else:
        hours = int(env("LOOKBACK_HOURS", "24"))
        mails = []
        for acc in load_accounts():
            try:
                got = fetch_account(acc, hours)
                print(f"{acc['name']}: {len(got)} messages", file=sys.stderr)
                mails += got
            except Exception as exc:
                msg = str(exc)
                for a in [acc.get("password", "")]:  # never echo secrets
                    if a:
                        msg = msg.replace(a, "***")
                errors.append(f"{acc['name']}: {type(exc).__name__}: {msg[:120]}")

    for m in mails:
        classify(m, rules)
    if env("LLM_MODE", "off").lower() == "gemini" and not args.sample:
        gemini_rerank(mails, rules)

    if args.dry_run:
        print(build_digest(mails, errors, show_reasons=args.reasons, html=args.html))
        return 1 if errors and not mails else 0
    use_html = delivery_mode() == "telegram"
    deliver(build_digest(mails, errors, html=use_html), html=use_html)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
