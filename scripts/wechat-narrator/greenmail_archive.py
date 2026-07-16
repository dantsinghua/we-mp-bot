#!/usr/bin/env python3
"""Incrementally archive GreenMail mailboxes to gzipped mbox on disk, then
expunge server messages older than RETAIN_DAYS (only ones already archived).
GreenMail is in-memory: this bounds its heap and preserves history across restarts.
Run daily via greenmail-archive.timer. State: mail-archive/state.json (UIDVALIDITY-aware)."""
import gzip, imaplib, json, os, re, time

from wn_logging import get_logger

HOST, PORT = "127.0.0.1", 3143
ACCOUNTS = ["wechat-group@test.local", "wechat-narrator@test.local"]
RETAIN_DAYS = 3  # ~150 msgs/day x ~1MB avg must fit the 768m JVM heap
ARCHIVE_DIR = os.path.expanduser("~/.wechat-narrator/mail-archive")
STATE = os.path.join(ARCHIVE_DIR, "state.json")
log = get_logger("mail_archive")


def run(account):
    st = state.setdefault(account, {})
    M = imaplib.IMAP4(HOST, PORT)
    M.login(account, account)
    typ, d = M.select("INBOX")
    typ, v = M.status("INBOX", "(UIDVALIDITY)")
    uidvalidity = int(v[0].split(b"UIDVALIDITY ")[1].rstrip(b")"))
    if st.get("uidvalidity") != uidvalidity:  # server wiped/recreated
        st.update(uidvalidity=uidvalidity, lastuid=0)
    typ, d = M.uid("search", None, f"UID {st['lastuid'] + 1}:*")
    uids = [int(u) for u in d[0].split() if int(u) > st["lastuid"]]
    if uids:
        out = os.path.join(ARCHIVE_DIR, f"{account.split('@')[0]}-{time.strftime('%Y%m')}.mbox.gz")
        with gzip.open(out, "ab") as f:
            for u in uids:
                typ, md = M.uid("fetch", str(u), "(RFC822)")
                raw = next(p[1] for p in md if isinstance(p, tuple))
                f.write(b"From archive %s\n" % time.strftime("%c").encode())
                f.write(raw.replace(b"\nFrom ", b"\n>From ") + b"\n\n")
        st["lastuid"] = max(uids)
        log.info("%s: archived %d msgs (uid<=%d) -> %s", account, len(uids), st["lastuid"], out)
    # expunge archived messages older than RETAIN_DAYS
    # (GreenMail lacks SEARCH BEFORE: fetch INTERNALDATE and filter locally)
    old = []
    if st["lastuid"]:
        cutoff = time.time() - RETAIN_DAYS * 86400
        typ, d = M.uid("fetch", f"1:{st['lastuid']}", "(INTERNALDATE)")
        for item in d:
            raw = item[0] if isinstance(item, tuple) else item
            m = raw and re.search(rb"UID (\d+)", raw)
            if m and b"INTERNALDATE" in raw and time.mktime(imaplib.Internaldate2tuple(raw)) < cutoff:
                old.append(m.group(1))
    if old:
        M.uid("store", b",".join(old), "+FLAGS", r"(\Deleted)")
        M.expunge()
        log.info("%s: expunged %d msgs older than %dd", account, len(old), RETAIN_DAYS)
    M.logout()


os.makedirs(ARCHIVE_DIR, exist_ok=True)
state = json.load(open(STATE)) if os.path.exists(STATE) else {}
for acct in ACCOUNTS:
    try:
        run(acct)
    except Exception:
        log.exception("%s: archive failed", acct)
json.dump(state, open(STATE, "w"), indent=1)
