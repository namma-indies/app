"""Public grievance intake and moderator handling (#199); no account needed to file."""
from html import escape
from typing import Literal
from urllib.parse import parse_qs, quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, ValidationError
from starlette.responses import HTMLResponse

from app.auth.deps import require_moderator
from app.config import settings
from app.deps import get_conn, get_storage
from app.grievances import Intake, KIND_LABELS, LEGAL_NOTE, clocks, linked_sighting, removal_hours
from app.ids import uuid7
from app.moderation_ops import action_reason, apply_sighting_action, record_action
from app.moderator_alerts import alert_moderators, queue_message
from app.photos import thumb_key
from app.ratelimit import Limit, check, client_key
from app.routes.join import _STYLE
from app.security import email_hash

router = APIRouter()
MAX_BODY = 128 * 1024


def _page(body: str, *, status=200, receipt=False) -> HTMLResponse:
    return HTMLResponse(f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{'<meta name="robots" content="noindex">' if receipt else ''}
<title>Grievances · IndieDex</title><style>{_STYLE}
.card {{ max-width:640px; }} select, textarea {{ width:100%; padding:12px; margin-bottom:16px;
border:1px solid var(--line); border-radius:10px; font:inherit; background:white; color:var(--ink); }}
textarea {{ resize:vertical; }} .small {{ font-size:.82rem; color:#6b5844; }}
.reference {{ overflow-wrap:anywhere; }} h2 {{ font-size:1.05rem; margin-top:1.5rem; }}
dl {{ font-size:.9rem; }} dt {{ font-weight:600; }} dd {{ margin:0 0 .7rem; white-space:pre-wrap; }}
</style></head><body><main class="card"><h1>Grievances</h1>{body}</main></body></html>''',
        status_code=status, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff", "Content-Security-Policy":
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"})


def _contact() -> str:
    name = escape(settings.grievance_officer_name.strip() or "Not yet appointed")
    email = settings.grievance_officer_email.strip()
    contact = escape(settings.grievance_officer_contact.strip())
    address = f'<a href="mailto:{quote(email, safe="@.")}">{escape(email)}</a>' if email else (contact or "Contact details pending")
    return f'<h2>Grievance Officer</h2><dl><dt>Name</dt><dd>{name}</dd><dt>Contact</dt><dd>{address}</dd>{f"<dd>{contact}</dd>" if contact and email else ""}</dl>'


def _form(*, values=None, error=None, status=200):
    values = values or {}
    def value(name):
        return escape(str(values.get(name, "")), quote=True)
    try:
        key = str(UUID(str(values.get("submission_key", ""))))
    except ValueError:
        key = str(uuid4())
    options = '<option value="" disabled selected>Choose a complaint type</option>' if values.get("kind") not in KIND_LABELS else ""
    options += "".join(f'<option value="{kind}" {"selected" if values.get("kind") == kind else ""}>{label}</option>' for kind, label in KIND_LABELS.items())
    banner = f'<p class="err" role="alert">{escape(error)}</p>' if error else ""
    deadlines = f'''<h2>Tracking targets</h2><p class="small">{LEGAL_NOTE}</p><dl>
<dt>All complaints</dt><dd>Acknowledge: {settings.grievance_acknowledge_hours:g} hours. Resolve: {settings.grievance_resolve_hours / 24:g} days.</dd>
<dt>Takedown or privacy</dt><dd>Removal review: {settings.grievance_removal_hours:g} hours.</dd>
<dt>Non-consensual intimate imagery</dt><dd>Urgent removal review: {settings.grievance_intimate_hours:g} hours.</dd>
<dt>Government or court order</dt><dd>Urgent action review: {settings.grievance_government_hours:g} hours.</dd></dl>'''
    return _page(f'''<p>You can send a complaint without an account.</p>{_contact()}{banner}
<h2>Send a complaint</h2><form method="post" action="/grievances">
<input type="hidden" name="submission_key" value="{key}">
<label for="kind">Complaint type</label><select id="kind" name="kind" required>{options}</select>
<label for="target">Link or sighting ID</label><input id="target" name="target" required maxlength="1000" value="{value('target')}">
<label for="details">Details</label><textarea id="details" name="details" required maxlength="5000" rows="5">{value('details')}</textarea>
<label for="contact_email">Contact email</label><input id="contact_email" name="contact_email" type="email" required maxlength="254" autocomplete="email" value="{value('contact_email')}">
<p class="small">Your details and contact email are shared with the moderation team to handle this complaint.
Include enough to identify the content. Please do not send or upload intimate imagery here.</p>
<button type="submit">Send complaint</button></form>{deadlines}<p><a href="/">Back to IndieDex</a></p>''', status=status)


def _receipt(reference: str, *, status=201):
    return _page(f'''<p class="ok">Your complaint has been received.</p>
<p>Reference: <strong class="reference">{escape(reference)}</strong></p>
<p>Keep this reference for follow-up. An acknowledgement email is queued; this receipt confirms your complaint was saved.</p>
{_contact()}<p><a href="/grievances">Back to grievances</a></p>''', status=status, receipt=True)


@router.get("/grievances", response_class=HTMLResponse)
async def grievance_page():
    return _form()


async def _read_form(request: Request) -> dict:
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/x-www-form-urlencoded":
        raise HTTPException(415, "Submit the grievance form")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY:
            raise HTTPException(413, "Complaint exceeds the form limit")
        body.extend(chunk)
    try:
        fields = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=8, errors="strict")
        if any(len(v) != 1 for v in fields.values()):
            raise ValueError("duplicate fields")
        return {k: v[0] for k, v in fields.items()}
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Invalid complaint form") from None


@router.post("/grievances", response_class=HTMLResponse)
async def submit_grievance(request: Request):
    check(f"grievance:ip:{client_key(request)}", Limit(settings.rl_grievance_ip_times, settings.rl_grievance_window_s))
    values = await _read_form(request)
    try:
        body = Intake.model_validate(values)
    except ValidationError:
        return _form(values=values, error="Check the type, link or sighting ID, details and contact email, then try again.", status=422)
    digest = body.digest()
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            # Retried submissions return the original receipt. They cannot
            # change evidence or queue another acknowledgement.
            existing = await conn.fetchrow("SELECT reference,payload_digest FROM grievances WHERE submission_key=$1", body.submission_key)
            if existing:
                if existing["payload_digest"] != digest:
                    return _form(values={**values, "submission_key": str(uuid4())}, error="This form was already submitted. Check the details and send a new complaint.", status=409)
                return _receipt(existing["reference"], status=200)
            check(f"grievance:email:{email_hash(body.contact_email)}", Limit(settings.rl_grievance_email_times, settings.rl_grievance_window_s))
            sid = linked_sighting(body.target)
            # Preserve the opaque reference even if content has already gone.
            # Intake neither validates nor discloses whether the target exists.
            gid, reference = uuid7(), "NI-" + uuid4().hex.upper()
            inserted = await conn.fetchval(
                "INSERT INTO grievances (id,reference,submission_key,payload_digest,kind,target,sighting_id,details,contact_email,"
                "acknowledge_due_at,resolve_due_at,removal_due_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,"
                "now()+$10*interval '1 hour',now()+$11*interval '1 hour',now()+$12*interval '1 hour') "
                "ON CONFLICT (submission_key) DO NOTHING RETURNING id",
                gid, reference, body.submission_key, digest, body.kind, body.target, sid, body.details, body.contact_email,
                float(settings.grievance_acknowledge_hours), float(settings.grievance_resolve_hours), removal_hours(body.kind))
            if inserted is None:
                existing = await conn.fetchrow("SELECT reference,payload_digest FROM grievances WHERE submission_key=$1", body.submission_key)
                if existing["payload_digest"] != digest:
                    raise HTTPException(409, "Submission key already used; reload the grievance form")
                return _receipt(existing["reference"], status=200)
            # Acknowledgements contain only the generated reference, never the
            # supplied link, evidence or contact address (anti-phishing/mail bomb).
            text = f"We received your IndieDex grievance.\n\nReference: {reference}\n\nKeep this reference for follow-up. The moderation team will review your complaint.\n"
            mail_id = await queue_message(conn, dedupe_key=f"grievance-ack:{gid}", recipient=body.contact_email,
                                          subject=f"IndieDex grievance received: {reference}", text=text,
                                          html=f"<p>We received your IndieDex grievance.</p><p>Reference: <strong>{reference}</strong></p><p>Keep this reference for follow-up. The moderation team will review your complaint.</p>", urgent=True)
            await conn.execute("UPDATE grievances SET acknowledgement_mail_id=$2 WHERE id=$1", gid, mail_id)
            await alert_moderators(conn, event_key=f"grievance:{gid}", category=f"grievance_{body.kind}",
                                   subject_id=gid, urgent=body.kind != "other")
    return _receipt(reference)


@router.get("/moderation/grievances")
async def grievance_queue(response: Response, include_closed: bool = False, reference: str | None = Query(None, max_length=40),
                          _mod: UUID = Depends(require_moderator), conn=Depends(get_conn), storage=Depends(get_storage)):
    response.headers["Cache-Control"] = "no-store"
    rows = await conn.fetch(
        "SELECT g.*,m.sent_at AS acknowledged_at,m.last_error AS acknowledgement_error, "
        "ARRAY(SELECT p.s3_key FROM photos p WHERE p.sighting_id=g.sighting_id ORDER BY p.created_at,p.id) AS photo_keys "
        "FROM grievances g "
        "LEFT JOIN moderation_mail m ON m.id=g.acknowledgement_mail_id "
        "WHERE ($1 OR g.resolved_at IS NULL OR m.sent_at IS NULL OR $2::text IS NOT NULL) "
        "AND ($2::text IS NULL OR g.reference=$2) "
        "ORDER BY (g.kind IN ('intimate_imagery','government_order')) DESC, "
        "LEAST(CASE WHEN m.sent_at IS NULL THEN g.acknowledge_due_at END,"
        "CASE WHEN g.resolved_at IS NULL THEN g.resolve_due_at END,"
        "CASE WHEN g.resolved_at IS NULL AND g.removed_at IS NULL THEN g.removal_due_at END),g.received_at,g.id LIMIT 200",
        include_closed, reference.strip().upper() if reference else None)
    keys = list(dict.fromkeys(key for r in rows for original in r["photo_keys"] for key in (thumb_key(original), original)))
    signed = dict(zip(keys, await storage.urls(keys)))
    return {"items": [{**{k: v for k, v in dict(r).items() if k not in {"payload_digest", "submission_key", "photo_keys"}},
                       "photos": [{"thumb_url": signed[thumb_key(k)], "photo_url": signed[k]} for k in r["photo_keys"]],
                       **clocks(r)} for r in rows], "legal_note": LEGAL_NOTE}


class GrievanceAction(BaseModel):
    action: Literal["hide", "resolve", "dismiss"]
    reason: str = Field(min_length=1, max_length=500)
    sighting_id: UUID | None = None


@router.post("/moderation/grievances/{grievance_id}/action")
async def decide_grievance(grievance_id: UUID, body: GrievanceAction,
                           moderator_id: UUID = Depends(require_moderator), conn=Depends(get_conn)):
    reason = action_reason(body.reason)
    async with conn.transaction():
        row = await conn.fetchrow("SELECT * FROM grievances WHERE id=$1 FOR UPDATE", grievance_id)
        if row is None:
            raise HTTPException(404, "no such grievance")
        if row["resolved_at"] is not None:
            raise HTTPException(409, "This complaint already has a decision")
        sid = body.sighting_id or row["sighting_id"]
        if body.action == "hide":
            if row["removed_at"] is not None and sid != row["sighting_id"]:
                raise HTTPException(409, "A removal is already recorded for a different sighting")
            if sid is None:
                raise HTTPException(422, "A sighting ID is needed to hide the linked content")
            await apply_sighting_action(conn, sighting_id=sid, verdict="rejected", moderator_id=moderator_id,
                                        reason=reason, action="hide")
            await conn.execute("UPDATE grievances SET sighting_id=$2,removed_at=COALESCE(removed_at,now()),"
                               "status='in_review',updated_at=now() WHERE id=$1", grievance_id, sid)
            new_state, audit_action = "in_review", "hide"
        else:
            new_state = "resolved" if body.action == "resolve" else "dismissed"
            audit_action = "approve" if body.action == "resolve" else "dismiss"
            await conn.execute("UPDATE grievances SET status=$2,resolved_at=now(),resolved_by=$3,"
                               "resolution_reason=$4,updated_at=now() WHERE id=$1",
                               grievance_id, new_state, moderator_id, reason)
        await record_action(conn, moderator_id=moderator_id, target_kind="grievance", target_id=grievance_id,
                            action=audit_action, reason=reason, previous_state=row["status"], new_state=new_state)
    return {"status": new_state}
