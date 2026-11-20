"""The monthly credit review report: a small PDF of a month's cases, kept outside the service's own database.

Built only from the cases and reviews tables. No agent and no model is called, so the same month gives the same
figures every time and a report costs nothing. Each closed case's review already holds what the specialists found.

Why it exists: a limit cut or a card pause is an adverse action on an existing account, and its records are kept
for 12 months for business credit (Reg B, 12 CFR 1002.12(b)(2); 60 days for a business with over $1M in revenue,
unless it asks, under (b)(5)); credit decisions are reviewed independently and on an ongoing basis (Interagency
Guidance on Credit Risk Review Systems, 2020). The POC drops and reseeds its database on a schema change, so the
copy lives elsewhere: S3 with Object Lock on AWS, a folder elsewhere (runtime.reports_storage). A stored copy is
never overwritten. Running a month again adds a copy.

A month is a calendar month in UTC, read on the wall clock (closed_at, created_at), not on the data clock the
signals carry. Seeded reviews are left out: they are history the demo starts from, not decisions made here.

The file is PDF/A-3U (ISO 19005-3), the archival form of PDF. Its fonts are embedded (Liberation Sans, in app/fonts,
under the SIL Open Font License), so it reads the same years from now with no app or font installed. Its figures
and rows ride inside it as report.json, so one locked object holds the page for people and the data for machines.

Two callers, one function: POST /reports/monthly/{YYYY-MM} (app/api/reports.py) and the service's hourly look
(run_due, from app/main.py), which makes last month's report when none is stored yet.
"""

import hashlib
import json
import logging
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from fpdf import FPDF
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, aliased

from app import runtime
from app.db import SessionLocal
from app.db.models import OPEN_STATUSES, Case, CaseStatus, Review

log = logging.getLogger(__name__)
# fpdf2 cuts the embedded fonts down to the glyphs used; fontTools logs every step of that at INFO.
logging.getLogger("fontTools").setLevel(logging.WARNING)

_MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
PREFIX = "monthly"
FORMAT = "PDF/A-3U"
FONTS = Path(__file__).parent / "fonts"
LINK_SECONDS = 900  # how long the download link in the API's answer works


class BadMonth(ValueError):
    """Not a YYYY-MM month, or one that has not begun."""


class AlreadyStored(RuntimeError):
    """A copy with this exact name is there already: two runs in the same second."""


class NoStorage(RuntimeError):
    """Neither REPORTS_BUCKET nor REPORTS_DIR is set."""


def month_range(month: str) -> tuple[datetime, datetime]:
    """The month's first instant and the next month's, in UTC."""
    if not _MONTH.fullmatch(month or ""):
        raise BadMonth(f"{month!r} is not a month written YYYY-MM")
    year, number = int(month[:4]), int(month[5:])
    return datetime(year, number, 1, tzinfo=UTC), datetime(year + number // 12, number % 12 + 1, 1, tzinfo=UTC)


def previous_month(now: datetime) -> str:
    year, number = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    return f"{year:04d}-{number:02d}"


def build(session: Session, month: str, now: datetime) -> dict:
    """The month's figures and one row per case closed in it."""
    start, end = month_range(month)
    closed = session.scalars(
        select(Review)
        .where(Review.source == "case", Review.closed_at >= start, Review.closed_at < end)
        .order_by(Review.closed_at, Review.case_id)
    ).all()
    # A seeded review comes with a case of its own, made when the service seeded it: that is no case opened here.
    seed = aliased(Review)
    seeded = exists().where(seed.case_id == Case.case_id, seed.source == "seed")
    in_month = (Case.created_at >= start, Case.created_at < end, ~seeded)
    opened = session.scalar(select(func.count()).select_from(Case).where(*in_month))
    still_open = session.scalar(
        select(func.count()).select_from(Case).where(*in_month, Case.status.in_(OPEN_STATUSES))
    )
    # A case that failed before its report was checked leaves no review (history.record), only its status.
    failed_unreported = session.scalar(
        select(func.count())
        .select_from(Case)
        .outerjoin(Review, Review.case_id == Case.case_id)
        .where(*in_month, Case.status == CaseStatus.FAILED, Review.case_id.is_(None))
    )

    rows, actions, removed_cents, people = [], Counter(), 0, Counter()
    for review in closed:
        action = review.action or {}
        kind = action.get("type")
        if kind == "reduce_limit" and review.outcome == "done":
            removed_cents += max(review.current_limit_cents - action.get("limit_cents", review.current_limit_cents), 0)
        actions[_action_label(kind, review.outcome)] += 1
        people[_people_label(review)] += 1
        rows.append({
            "case_id": review.case_id,
            "customer_id": review.customer_id,
            "closed": review.closed_at.strftime("%m-%d"),
            "outcome": review.outcome,
            "cause": review.likely_cause or "-",
            "recommended": review.recommendation or "-",
            "people": _who(review.decisions or []),
            "action": _action_label(kind, review.outcome),
        })
    return {
        "month": month,
        "month_to_date": start <= now < end,
        "totals": {
            "opened": opened,
            "closed": len(closed),
            "still_open": still_open,
            "failed_before_a_report": failed_unreported,
            "outcomes": dict(Counter(review.outcome for review in closed)),
            "report_check_failed": sum(not review.check_passed for review in closed),
            "actions": dict(actions),
            "limit_removed_cents": removed_cents,
            "people": dict(people),
        },
        "policy_versions": sorted({review.policy_version for review in closed if review.policy_version}),
        "rows": rows,
    }


def _action_label(kind: str | None, outcome: str) -> str:
    if kind is None:
        return "no change"
    # failed: approved, but Card did not confirm it (cases._close_unconfirmed)
    return {"reduce_limit": "limit cut", "pause_card": "card paused"}.get(kind, kind) + (
        "" if outcome == "done" else " (not confirmed)"
    )


def _people_label(review: Review) -> str:
    if review.recommendation == "needs_human":
        return "handed to a person"
    if any(decision.get("decision") == "reject" for decision in review.decisions or []):
        return "rejected"
    return "approved as recommended"


def _who(decisions: list[dict]) -> str:
    approved = [decision["by"] for decision in decisions if decision.get("decision") == "approve" and "by" in decision]
    rejected = [decision["by"] for decision in decisions if decision.get("decision") == "reject" and "by" in decision]
    decided = [f"decided {decision['decided']}" for decision in decisions if decision.get("decided")]
    parts = ([", ".join(approved)] if approved else []) + ([f"rejected by {', '.join(rejected)}"] if rejected else [])
    return "; ".join(parts + decided) or "-"


def render(report: dict) -> bytes:
    """One page as a rule, landscape A4. Ids, labels and amounts only: no names, notes or evidence."""
    totals, generated = report["totals"], report["generated"]
    start, _ = month_range(report["month"])
    title = f"{start:%B %Y}" + (" (month to date)" if report["month_to_date"] else "")

    made = datetime.fromisoformat(generated["at"])
    # PDF/A-3U: fpdf2 refuses whatever the standard forbids, and adds the sRGB output intent and the XMP itself.
    pdf = FPDF(orientation="L", format="A4", enforce_compliance="PDFA_3U")
    pdf.set_creation_date(made)
    pdf.set_title(f"Credit review monthly report {report['month']}")
    pdf.set_lang("en-US")
    # Embedded, as PDF/A requires. Liberation Sans has Helvetica's widths, so the layout is the one drawn before.
    for style, name in (("", "Regular"), ("B", "Bold"), ("I", "Italic")):
        pdf.add_font("Sans", style, FONTS / f"LiberationSans-{name}.ttf")
    pdf.embed_file(bytes=json.dumps(report, indent=1, sort_keys=True).encode(), basename="report.json",
                   mime_type="application/json", associated_file_relationship="Data", modification_date=made,
                   desc="The figures and rows of this report, for machines")
    pdf.set_auto_page_break(True, margin=12)
    pdf.add_page()

    def line(text: str, *, size: float = 9, style: str = "", height: float = 5.5) -> None:
        pdf.set_font("Sans", style, size)
        pdf.cell(0, height, text, new_x="LMARGIN", new_y="NEXT")

    line(f"Credit review — monthly report, {title}", size=14, style="B", height=8)
    line(f"Generated {generated['at'][:16].replace('T', ' ')} UTC · {generated['how']}, by {generated['by']} · "
         f"months are UTC · policy {', '.join(report['policy_versions']) or '-'}", size=8)
    pdf.ln(3)
    for label, text in (
        ("Cases", f"opened {totals['opened']} · closed {totals['closed']} · of those opened, still open "
                  f"{totals['still_open']} and failed before a report {totals['failed_before_a_report']}"),
        ("Outcomes", _counts(totals["outcomes"])
                     + (f" · report check failed {totals['report_check_failed']}"
                        if totals["report_check_failed"] else "")),
        ("Actions", _counts(totals["actions"])
                    + (f" · ${totals['limit_removed_cents'] / 100:,.0f} of limit removed"
                       if totals["limit_removed_cents"] else "")),
        ("People", _counts(totals["people"])),
    ):
        pdf.set_font("Sans", "B", 9)
        pdf.cell(24, 5.5, label)
        line(text)
    pdf.ln(3)

    if report["rows"]:
        pdf.set_font("Sans", size=8)
        headings = ("Case", "Customer", "Closed", "Outcome", "Cause", "Recommended", "Decided by", "Action")
        with pdf.table(col_widths=(34, 18, 13, 17, 40, 28, 70, 30), text_align="LEFT", line_height=4.5) as table:
            for values in (headings, *(tuple(row[k] for k in (
                "case_id", "customer_id", "closed", "outcome", "cause", "recommended", "people", "action"
            )) for row in report["rows"])):
                cells = table.row()
                for value in values:
                    cells.cell(str(value))
    else:
        line("No case was closed in this month.", style="I")
    pdf.ln(3)
    line("Built from the Orchestrator's records only. No model was called.", size=7, style="I")
    return bytes(pdf.output())


def _counts(counts: dict) -> str:
    return " · ".join(f"{name} {count}" for name, count in sorted(counts.items())) or "none"


def _key(month: str, now: datetime, how: str) -> str:
    return f"{PREFIX}/{month}/credit-review-{month}-{now:%Y%m%dT%H%M%SZ}-{how}.pdf"


def store(month: str, pdf: bytes, *, now: datetime, how: str, by: str) -> dict:
    """Write one copy, never over another. Returns where it went, and a download link for one in S3."""
    bucket, folder = runtime.reports_storage()
    key, digest = _key(month, now, how), hashlib.sha256(pdf).hexdigest()
    if bucket:
        from botocore.exceptions import ClientError

        s3 = _s3()
        try:
            s3.put_object(
                Bucket=bucket, Key=key, Body=pdf, ContentType="application/pdf", IfNoneMatch="*",
                Metadata={"sha256": digest, "generated-by": by.encode("ascii", "replace").decode(), "format": FORMAT},
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in ("PreconditionFailed", "ConditionalRequestConflict"):
                raise AlreadyStored(key) from error
            raise
        url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=LINK_SECONDS)
        return {"key": key, "location": f"s3://{bucket}/{key}", "url": url, "sha256": digest}
    if folder:
        path = Path(folder) / key
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as out:  # x: fails rather than overwrite
                out.write(pdf)
        except FileExistsError as error:
            raise AlreadyStored(key) from error
        return {"key": key, "location": str(path), "url": None, "sha256": digest}
    raise NoStorage("Set REPORTS_BUCKET or REPORTS_DIR to keep monthly reports")


def stored(month: str) -> bool:
    """Whether any copy of this month's report is kept."""
    bucket, folder = runtime.reports_storage()
    prefix = f"{PREFIX}/{month}/"
    if bucket:
        return _s3().list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1).get("KeyCount", 0) > 0
    if folder:
        return any((Path(folder) / prefix).glob("*.pdf"))
    raise NoStorage("Set REPORTS_BUCKET or REPORTS_DIR to keep monthly reports")


def _s3():
    import boto3  # here, so a service with no bucket set never loads it

    # Credentials and region come from boto3's own chain: the instance role on AWS, AWS_PROFILE on a laptop.
    # The region's own endpoint: on the global one (bucket.s3.amazonaws.com) a download link for a bucket outside
    # us-east-1 is refused while that bucket is new ("the region 'us-east-2' is wrong"). Uploads follow a redirect.
    # With an endpoint given, SigV4 and bucket-in-host links must be asked for: else the links are SigV2 and path-style.
    from botocore.config import Config

    session = boto3.session.Session()
    region = session.region_name
    return session.client(
        "s3",
        endpoint_url=f"https://s3.{region}.amazonaws.com" if region else None,
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


def generate(month: str, *, by: str, now: datetime | None = None, scheduled: bool = False) -> dict:
    """Build, draw and keep one month's report. Returns where it is, its sha256 and its figures."""
    now = now or datetime.now(UTC)
    start, _ = month_range(month)
    if start > now:
        raise BadMonth(f"{month} has not begun")
    how = "scheduled" if scheduled else "manual"
    with SessionLocal() as session:
        report = build(session, month, now)
    report["generated"] = {"at": now.isoformat(timespec="seconds"), "by": by, "how": how}
    where = store(month, render(report), now=now, how=how, by=by)
    log.info("Monthly report %s kept at %s (%s, by %s)", month, where["location"], how, by)
    return {"month": month, "month_to_date": report["month_to_date"], **where, "generated_at": now,
            "generated_by": by, "totals": report["totals"]}


def run_due(now: datetime | None = None) -> dict | None:
    """Make last month's report if none is kept yet. The service calls this at start and then every so often, so a
    service that was down on the 1st catches up when it starts. None when there was nothing to do."""
    now = now or datetime.now(UTC)
    month = previous_month(now)
    if stored(month):
        return None
    return generate(month, by="scheduler", now=now, scheduled=True)
