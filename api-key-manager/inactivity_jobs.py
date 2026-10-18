"""Inactive accounts — daily job (cron 06:20).

  cd /root/phixtra-app/api-key-manager && ./venv/bin/python3 inactivity_jobs.py

Mode is set on the admin Inactive Accounts page:
  off  — does nothing
  test — sends nothing to businesses; emails PhiXtra's admins what it WOULD do
  on   — sends the warnings, deletes accounts whose date has come, and emails
         PhiXtra's admins a summary
Rules and dates live in inactivity_core.py.
"""

from __future__ import annotations

import os
from html import escape

import inactivity_core as ic
from portal_utils import send_email

ACTION_LABELS = {"warn1": "First warning", "warn2": "Second warning", "warn3": "Final warning",
                 "delete": "Deleted"}


def run():
    mode = ic.get_mode()
    if mode == "off":
        print("inactivity: off")
        return
    ic.purge_old_log()
    done = []
    for r in ic.accounts():
        if r["reset"] and mode == "on":
            ic.clear_warnings(r["tenant_id"])
            ic.log(r["tenant_id"], r["business_name"], r["email"], "reset",
                   "Active again — warnings cancelled", mode)
        if not r["due"]:
            continue
        line = {"r": r, "action": r["due"], "ok": True, "detail": ""}
        if mode == "test":
            line["detail"] = f"would be deleted on {ic._d(r['deletion_on'])}" if r["due"] != "delete" else "would be deleted today"
            done.append(line)
            continue
        try:
            if r["due"] == "delete":
                to, row = r["email"], dict(r)
                res = ic.delete_business(r["tenant_id"], "inactivity job")
                subject, html = ic.email_html("deleted", row)
                sent = send_email(to, subject, html, reply_to="support@phixtra.com")
                line["detail"] = f"{sum(res['rows_removed'].values())} records, {res['files_removed']} files removed" + \
                                 ("" if sent else " · goodbye email FAILED")
            else:
                subject, html = ic.email_html(r["due"], r)
                if not send_email(r["email"], subject, html, reply_to="support@phixtra.com"):
                    raise RuntimeError("email didn't send — will try again tomorrow")
                ic.record_warning(r, int(r["due"][-1]))
                line["detail"] = f"deletion on {ic._d(r['deletion_on'])}"
        except Exception as e:
            line.update(ok=False, detail=str(e)[:300])
        ic.log(r["tenant_id"], r["business_name"], r["email"],
               r["due"] if line["ok"] else r["due"] + "_failed", line["detail"], mode)
        done.append(line)
    print(f"inactivity: mode={mode} actions={len(done)}")
    if done:
        _email_admins(mode, done)


def _email_admins(mode, done):
    to = [a.strip() for a in os.getenv("ADMIN_EMAILS", "").split(",") if a.strip()]
    if not to:
        return
    rows = "".join(
        f"<tr><td style='padding:6px 8px;border-bottom:1px solid #e5e7eb'>{escape(x['r']['business_name'] or '—')}</td>"
        f"<td style='padding:6px 8px;border-bottom:1px solid #e5e7eb'>{escape(x['r']['email'] or '')}</td>"
        f"<td style='padding:6px 8px;border-bottom:1px solid #e5e7eb'>{ACTION_LABELS[x['action']]}"
        f"{'' if x['ok'] else ' — <b style=color:#b91c1c>FAILED</b>'}</td>"
        f"<td style='padding:6px 8px;border-bottom:1px solid #e5e7eb'>{escape(x['detail'])}</td></tr>"
        for x in done)
    title = ("TEST MODE — nothing was sent or deleted. This is what would have happened today."
             if mode == "test" else "What happened today.")
    html = (f"<div style='font-family:Arial,sans-serif;font-size:13px;color:#0f172a'>"
            f"<h2 style='font-size:17px'>Inactive accounts</h2><p>{title}</p>"
            f"<table style='border-collapse:collapse;font-size:13px'><tr>"
            f"<th align=left style='padding:6px 8px'>Business</th><th align=left style='padding:6px 8px'>Email</th>"
            f"<th align=left style='padding:6px 8px'>Step</th><th align=left style='padding:6px 8px'>Detail</th></tr>"
            f"{rows}</table>"
            f"<p>Keep or delay any account on the admin portal: Customers → Inactive Accounts.</p></div>")
    subject = f"{'[TEST] ' if mode == 'test' else ''}Inactive accounts: {len(done)} today"
    for addr in to:
        send_email(addr, subject, html)


if __name__ == "__main__":
    run()
