"""
wa_plan_reset.py — Monthly/annual billing period reset.

Scheduled daily at 00:05 UTC by the gateway scheduler.

Logic:
  - Monthly tenants: if plan_period_start is in a past month, reset to
    the 1st of the current month and clear quota_notified_at.
  - Annual tenants: if plan_period_start + 1 year <= today, reset to
    today and clear quota_notified_at.

This is what makes the AI message quota "refresh" each billing period.
"""

from datetime import date, datetime
from fastapi import APIRouter, Header, HTTPException
import html as _html
import json
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import psycopg2
import psycopg2.extras

from wa_db import get_db_connection

router = APIRouter()

_INTERNAL_TOKEN  = os.getenv("PHIXTRA_INTERNAL_TOKEN", "")
_PORTAL_BASE_URL = os.getenv("PORTAL_BASE_URL", "https://portal.phixtra.com").rstrip("/")
_BRAND           = "#030C18"

# Keep in sync with portal_routes.py::_build_free_features(). Duplicated here
# because whatsapp-gateway and api-key-manager are separate deployable
# services with no shared Python package. All flags off — a tenant earns
# them back through _grant_trial_upgrade() on its next real connection.
_FREE_PLAN_FEATURES_JSON = json.dumps({
    "product_recommendation":    False,
    "related_products":          False,
    "cart_recovery":             False,
    "verified_specs_web_lookup": False,
    "chat_archive_unlimited":    False,
})


def _send_plain_email(to_email: str, subject: str, html: str, text: str = "") -> None:
    """Best-effort SMTP send — never raises."""
    try:
        smtp_host = os.getenv("SMTP_HOST", "")
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        smtp_user = os.getenv("SMTP_USER", "")
        # The .env names it SMTP_PASSWORD (as the portal and meta_webhook.py
        # read it); SMTP_PASS was never set, so these emails silently failed.
        smtp_pass = os.getenv("SMTP_PASSWORD", "") or os.getenv("SMTP_PASS", "")
        from_addr = os.getenv("SMTP_FROM", smtp_user)
        if not smtp_host or not smtp_user:
            return
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = from_addr
        msg["To"]      = to_email
        if text:
            msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as s:
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.sendmail(from_addr, [to_email], msg.as_string())
    except Exception as e:
        print(f"⚠️ [EMAIL] send failed to {to_email}: {e}")


def _send_founder_year2_email(email: str, first_name: str, business_name: str) -> None:
    """Notify a founder that their free Year 1 has ended and Year 2 rates now apply."""
    greeting    = first_name.strip() if first_name and first_name.strip() else "there"
    subscribe_url = f"{_PORTAL_BASE_URL}/billing/subscribe"
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto">
      <div style="background:{_BRAND};padding:20px 24px;border-radius:12px 12px 0 0">
        <p style="color:#25D366;font-size:11px;font-weight:800;letter-spacing:.1em;
                  text-transform:uppercase;margin:0 0 6px">Founder&#39;s Offer — Year 2</p>
        <h2 style="color:#fff;margin:0;font-size:22px">Your free year has ended.</h2>
      </div>
      <div style="border:1px solid #e5e7eb;border-top:none;border-radius:0 0 12px 12px;padding:24px">
        <p>Hi {greeting},</p>
        <p>Your Founder Year 1 for <b>{business_name}</b> has now ended. As a Founder,
           your Year 2 is at <b>50% off</b> the standard annual rate.</p>
        <table style="width:100%;border-collapse:collapse;margin:16px 0">
          <tr>
            <td style="padding:9px 12px;background:#f3f4f6;border:1px solid #e5e7eb;font-weight:700;width:110px">Starter</td>
            <td style="padding:9px 12px;border:1px solid #e5e7eb">
                &#8358;7,125/mo &mdash; billed as &#8358;85,500/yr</td>
          </tr>
          <tr>
            <td style="padding:9px 12px;background:#f3f4f6;border:1px solid #e5e7eb;font-weight:700">Growth</td>
            <td style="padding:9px 12px;border:1px solid #e5e7eb">
                &#8358;22,800/mo &mdash; billed as &#8358;273,600/yr</td>
          </tr>
        </table>
        <p>Your account has been moved to the Free plan while you choose your Year 2 plan.
           Your data and settings are fully intact.</p>
        <p>
          <a href="{subscribe_url}"
             style="display:inline-block;background:{_BRAND};color:#fff;padding:12px 22px;
                    border-radius:12px;text-decoration:none;font-weight:700;font-size:15px">
            Subscribe to Year 2
          </a>
        </p>
        <p style="color:#6b7280;font-size:13px;margin-top:20px">
          Questions? Contact
          <a href="mailto:support@phixtra.com" style="color:{_BRAND}">support@phixtra.com</a>
        </p>
      </div>
    </div>"""
    _send_plain_email(
        to_email=email,
        subject="Your PhiXtra Founder Year 1 has ended — Year 2 at 50% off",
        html=html,
        text=(
            f"Hi {greeting},\n\n"
            f"Your free Founder Year 1 for {business_name} has ended.\n\n"
            f"Year 2 Founder rates (50% off):\n"
            f"  Starter: ₦7,125/mo (₦85,500/yr)\n"
            f"  Growth:  ₦22,800/mo (₦273,600/yr)\n\n"
            f"Your account is on the Free plan until you subscribe.\n"
            f"Subscribe: {subscribe_url}\n\n"
            f"Questions? support@phixtra.com"
        ),
    )


# ── WhatsApp 2-week AI trial (2026-09-25) ────────────────────────────────────
# A WhatsApp merchant starts on PhiXtra Connect (no AI) and can start one
# 2-week AI trial on Enterprise from the portal. This emails them 3 days
# before and on the last day, and when the trial ends unpaid moves them back
# to PhiXtra Connect with the AI switched off. Paying clears trial_ends_at,
# so a paying merchant is never picked up here. WooCommerce is untouched.

def _trial_email(to_email: str, first_name: str, subject: str, heading: str,
                 paragraphs: list, button: str, text: str) -> None:
    greeting = first_name.strip() if first_name and first_name.strip() else "there"
    plans_url = f"{_PORTAL_BASE_URL}/billing/plans"
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto">
      <div style="background:{_BRAND};padding:20px 24px;border-radius:12px 12px 0 0">
        <p style="color:#25D366;font-size:11px;font-weight:800;letter-spacing:.1em;
                  text-transform:uppercase;margin:0 0 6px">PhiXtra AI trial</p>
        <h2 style="color:#fff;margin:0;font-size:22px">{heading}</h2>
      </div>
      <div style="border:1px solid #e5e7eb;border-top:none;border-radius:0 0 12px 12px;padding:24px">
        <p>Hi {greeting},</p>
        {body}
        <p>
          <a href="{plans_url}"
             style="display:inline-block;background:#12B76A;color:#fff;padding:12px 22px;
                    border-radius:12px;text-decoration:none;font-weight:700;font-size:15px">
            {button}
          </a>
        </p>
        <p style="color:#6b7280;font-size:13px;margin-top:20px">
          Questions? Contact
          <a href="mailto:support@phixtra.com" style="color:{_BRAND}">support@phixtra.com</a>
        </p>
      </div>
    </div>"""
    _send_plain_email(to_email=to_email, subject=subject, html=html,
                      text=f"Hi {greeting},\n\n{text}\n\nChoose a plan: {plans_url}\n\n"
                           f"Questions? support@phixtra.com")


def _wa_trial_owner(cur, tenant_id: int):
    cur.execute("""
        SELECT c.email, c.first_name FROM customers c
         WHERE c.tenant_id = %s AND c.email_verified = TRUE AND c.is_active = TRUE
         ORDER BY c.id LIMIT 1
    """, (tenant_id,))
    return cur.fetchone()


def _run_wa_ai_trials(conn) -> dict:
    out = {"wa_trial_3d": 0, "wa_trial_last_day": 0, "wa_trials_ended": 0}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        # Reminders. trial_ends_at is the day the trial is switched off, so
        # "3 days left" = 3 days before it, "last day" = the day before it.
        for days, col, key in ((3, "trial_reminder_3d_at", "wa_trial_3d"),
                               (1, "trial_reminder_0d_at", "wa_trial_last_day")):
            cur.execute(f"""
                SELECT id, name, trial_ends_at FROM tenants
                 WHERE source_type = 'whatsapp' AND trial_ends_at IS NOT NULL
                   AND trial_ends_at - CURRENT_DATE = %s AND {col} IS NULL
            """, (days,))
            for t in cur.fetchall() or []:
                owner = _wa_trial_owner(cur, int(t["id"]))
                business = t["name"] or "your business"
                if owner and owner.get("email"):
                    if days == 3:
                        _trial_email(
                            owner["email"], owner.get("first_name") or "",
                            subject="Your PhiXtra AI trial ends in 3 days",
                            heading="3 days left on your AI trial",
                            paragraphs=[
                                f"Your 2-week AI trial for <b>{business}</b> ends in <b>3 days</b>.",
                                "Choose a plan now to keep the AI answering your WhatsApp customers. "
                                "If you don't, your AI features will lock and your account goes back "
                                "to PhiXtra Connect, where only your staff can reply.",
                                "Your chats, contacts and AI settings are always kept.",
                            ],
                            button="Choose a plan",
                            text=(f"Your 2-week AI trial for {business} ends in 3 days. Choose a plan "
                                  "to keep the AI answering your WhatsApp customers. If you don't, "
                                  "your AI features lock and you go back to PhiXtra Connect."))
                    else:
                        _trial_email(
                            owner["email"], owner.get("first_name") or "",
                            subject="Your PhiXtra AI trial ends today",
                            heading="Your AI trial ends today",
                            paragraphs=[
                                f"Today is the last day of your 2-week AI trial for <b>{business}</b>.",
                                "From tomorrow the AI stops answering your WhatsApp customers and your "
                                "AI features lock, unless you choose a plan today.",
                                "Your chats, contacts and AI settings are always kept.",
                            ],
                            button="Keep my AI — choose a plan",
                            text=(f"Today is the last day of your 2-week AI trial for {business}. From "
                                  "tomorrow the AI stops answering and your AI features lock, unless "
                                  "you choose a plan today."))
                cur.execute(f"UPDATE tenants SET {col} = NOW() WHERE id = %s", (int(t["id"]),))
                conn.commit()
                out[key] += 1

        # Trial over and not paid: back to PhiXtra Connect, AI off.
        cur.execute("""
            UPDATE tenants
               SET plan_id           = (SELECT id FROM plans WHERE slug='connect' LIMIT 1),
                   ai_enabled        = FALSE,
                   trial_ends_at     = NULL,
                   quota_notified_at = NULL,
                   features          = %s
             WHERE source_type = 'whatsapp'
               AND trial_ends_at IS NOT NULL
               AND trial_ends_at <= CURRENT_DATE
               AND EXISTS (SELECT 1 FROM plans WHERE slug='connect')
         RETURNING id, name
        """, (_FREE_PLAN_FEATURES_JSON,))
        ended = cur.fetchall() or []
        conn.commit()
        out["wa_trials_ended"] = len(ended)

        # Back on PhiXtra Connect: only the longest-serving staff up to its
        # seats stay active; the rest are switched off (never deleted).
        switched_off = {}
        if ended:
            cur.execute("""
                UPDATE team_members tm SET is_active = FALSE
                  FROM (SELECT m.id,
                               ROW_NUMBER() OVER (PARTITION BY m.tenant_id
                                                  ORDER BY m.created_at NULLS LAST, m.id) AS rn,
                               COALESCE(p.staff_limit, 0) AS lim
                          FROM team_members m
                          JOIN tenants t ON t.id = m.tenant_id
                          JOIN plans   p ON p.id = t.plan_id
                         WHERE m.is_active AND t.id = ANY(%s)
                           AND t.source_type = 'whatsapp' AND p.slug = 'connect') r
                 WHERE tm.id = r.id AND r.rn > r.lim
                RETURNING tm.tenant_id, tm.id
            """, ([int(t["id"]) for t in ended],))
            for r in cur.fetchall() or []:
                switched_off.setdefault(int(r["tenant_id"]), []).append(int(r["id"]))
            conn.commit()
            for tid, mids in switched_off.items():
                cur.execute("""
                    INSERT INTO audit_logs (admin_username, action, tenant_id, details)
                    VALUES ('system:wa_plan_reset', 'team_member_deactivated_plan_seats', %s, %s)
                """, (tid, json.dumps({"team_member_ids": mids, "reason": "2-week AI trial ended"})))
            conn.commit()

        for t in ended:
            try:
                owner = _wa_trial_owner(cur, int(t["id"]))
                business = t["name"] or "your business"
                team_line = []
                n_off = len(switched_off.get(int(t["id"]), []))
                if n_off:
                    cur.execute("SELECT name FROM team_members WHERE tenant_id=%s AND is_active ORDER BY created_at NULLS LAST, id",
                                (int(t["id"]),))
                    kept = [r["name"] for r in cur.fetchall() or [] if r.get("name")]
                    kept_txt = (", ".join(f"<b>{_html.escape(k)}</b>" for k in kept) + (" stays" if len(kept) == 1 else " stay")
                                + " active") if kept else "No staff stay active"
                    team_line = [
                        f"PhiXtra Connect includes a limited number of team seats, so {kept_txt} and "
                        f"{n_off} other staff member{'s were' if n_off != 1 else ' was'} switched off. "
                        "No one was deleted: you can swap who is active on the Team page, or upgrade "
                        "to switch everyone back on."
                    ]
                if owner and owner.get("email"):
                    _trial_email(
                        owner["email"], owner.get("first_name") or "",
                        subject="Your PhiXtra AI trial has ended",
                        heading="Your AI trial has ended",
                        paragraphs=[
                            f"Your 2-week AI trial for <b>{business}</b> has ended, and your account "
                            "is now on PhiXtra Connect.",
                            "The AI has stopped answering your WhatsApp customers and your AI features "
                            "are locked. Your staff can still reply from the Inbox as normal.",
                            "Your AI settings are saved. Choose a plan and the AI switches back on straight away.",
                        ] + team_line,
                        button="Upgrade to unlock",
                        text=(f"Your 2-week AI trial for {business} has ended and your account is now "
                              "on PhiXtra Connect. The AI has stopped answering and your AI features "
                              "are locked. Choose a plan to switch the AI back on."))
                cur.execute("UPDATE tenants SET trial_ended_email_at = NOW() WHERE id = %s", (int(t["id"]),))
                conn.commit()
            except Exception as e:
                print(f"⚠️ [PLAN RESET] trial-ended email failed tenant {t['id']}: {e}")
        if any(out.values()):
            print(f"✅ [PLAN RESET] WhatsApp AI trials: {out}")
    except Exception as e:
        conn.rollback()
        print(f"⚠️ [PLAN RESET] WhatsApp AI trial step error: {e}")
    finally:
        cur.close()
    return out


def run_plan_resets() -> dict:
    """
    Roll forward plan_period_start for all tenants whose period has lapsed.
    Returns {"monthly_reset": n, "annual_reset": n}.
    """
    conn = get_db_connection()
    if not conn:
        print("⚠️ [PLAN RESET] DB unavailable — skipping")
        return {"monthly_reset": 0, "annual_reset": 0}

    cur = conn.cursor()
    results = {"monthly_reset": 0, "annual_reset": 0, "trials_expired": 0}

    try:
        # ── Monthly: reset if period_start is before the 1st of this month ─────
        cur.execute("""
            UPDATE tenants
            SET plan_period_start = DATE_TRUNC('month', NOW())::DATE,
                quota_notified_at  = NULL
            WHERE billing_cycle = 'monthly'
              AND plan_id IS NOT NULL
              AND plan_period_start < DATE_TRUNC('month', NOW())::DATE
        """)
        results["monthly_reset"] = cur.rowcount

        # ── Annual: reset if period_start + 1 year <= today ──────────────────
        cur.execute("""
            UPDATE tenants
            SET plan_period_start = CURRENT_DATE,
                quota_notified_at  = NULL
            WHERE billing_cycle = 'annual'
              AND plan_id IS NOT NULL
              AND plan_period_start + INTERVAL '1 year' <= CURRENT_DATE
        """)
        results["annual_reset"] = cur.rowcount

        conn.commit()

        # ── WhatsApp 2-week AI trials: reminders + end → PhiXtra Connect ─────
        results.update(_run_wa_ai_trials(conn))

        # ── Founder Year 1 expiry: move to Year 2, downgrade to Free ────────
        # (WooCommerce only now — WhatsApp founders get the 2-week trial above.)
        # Fetch founders first so we can send transition emails
        cur.execute("""
            SELECT id, name, trial_ends_at
            FROM tenants
            WHERE is_founder = TRUE
              AND founder_year = 1
              AND COALESCE(source_type, 'web') <> 'whatsapp'
              AND trial_ends_at IS NOT NULL
              AND trial_ends_at <= CURRENT_DATE
        """)
        founders_transitioning = cur.fetchall() or []

        if founders_transitioning:
            founder_ids = [int(r[0]) for r in founders_transitioning]
            cur.execute("""
                UPDATE tenants
                SET founder_year      = 2,
                    plan_id           = (SELECT id FROM plans WHERE slug='free' LIMIT 1),
                    trial_ends_at     = NULL,
                    quota_notified_at = NULL,
                    features          = %s
                WHERE id = ANY(%s)
            """, (_FREE_PLAN_FEATURES_JSON, founder_ids))
            conn.commit()
            print(f"✅ [PLAN RESET] {len(founder_ids)} founder(s) moved to Year 2")

            # Notify each founder that their free year has ended and Year 2 rates apply
            import psycopg2.extras as _extras
            for row in founders_transitioning:
                tenant_id_f = int(row[0])
                business_name_f = row[1] or "your business"
                try:
                    cur_e = conn.cursor(cursor_factory=_extras.RealDictCursor)
                    cur_e.execute("""
                        SELECT c.email, c.first_name
                        FROM customers c
                        WHERE c.tenant_id = %s AND c.email_verified = TRUE AND c.is_active = TRUE
                        LIMIT 1
                    """, (tenant_id_f,))
                    contact = cur_e.fetchone()
                    cur_e.close()
                    if contact and contact.get("email"):
                        _send_founder_year2_email(
                            email=contact["email"],
                            first_name=contact.get("first_name") or "",
                            business_name=business_name_f,
                        )
                except Exception as email_err:
                    print(f"⚠️ [PLAN RESET] founder year2 email failed tenant {tenant_id_f}: {email_err}")

        results["founders_year2"] = len(founders_transitioning)

        # ── Regular trial expiry (WooCommerce): downgrade Pro trial to Free ──
        cur.execute("""
            UPDATE tenants
            SET plan_id       = (SELECT id FROM plans WHERE slug='free' LIMIT 1),
                trial_ends_at = NULL,
                quota_notified_at = NULL,
                features      = %s
            WHERE (is_founder = FALSE OR is_founder IS NULL)
              AND COALESCE(source_type, 'web') <> 'whatsapp'
              AND trial_ends_at IS NOT NULL
              AND trial_ends_at <= CURRENT_DATE
        """, (_FREE_PLAN_FEATURES_JSON,))
        results["trials_expired"] = cur.rowcount
        conn.commit()

        total = results["monthly_reset"] + results["annual_reset"] + results.get("trials_expired", 0) + results.get("founders_year2", 0) + results.get("wa_trials_ended", 0)
        if total:
            print(f"✅ [PLAN RESET] {results['monthly_reset']} monthly + {results['annual_reset']} annual + {results['trials_expired']} trials expired + {results.get('founders_year2', 0)} founders→year2")
        else:
            print("ℹ️ [PLAN RESET] No periods due for reset today")

    except Exception as e:
        conn.rollback()
        print(f"⚠️ [PLAN RESET] Error: {e}")
    finally:
        cur.close()
        conn.close()

    return results


# ── Manual trigger endpoint (admin / testing) ─────────────────────────────────

@router.post("/wa-plan-reset")
def trigger_plan_reset(authorization: str = Header(default="")):
    """
    Manually trigger billing period resets.
    Header: Authorization: Bearer {PHIXTRA_INTERNAL_TOKEN}
    """
    token = authorization.removeprefix("Bearer ").strip()
    if not _INTERNAL_TOKEN or token != _INTERNAL_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorised")

    results = run_plan_resets()
    return {"status": "ok", "results": results, "run_at": datetime.utcnow().isoformat()}
