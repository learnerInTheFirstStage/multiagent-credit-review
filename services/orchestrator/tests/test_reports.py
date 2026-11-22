"""The monthly credit review report (app/reports.py, app/api/reports.py). No network: S3 is moto's, Cloud Storage a
fake of the few calls made."""

import hashlib
import io
import json
from datetime import UTC, datetime

import pypdf
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws
from app import reports
from app.db import Base, SessionLocal, engine
from app.db.models import Case, Review
from app.main import app
from tests.conftest import bearer

NOV_23 = datetime(2026, 11, 23, 14, 5, tzinfo=UTC)
DEC_2 = datetime(2026, 12, 2, 0, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def empty_tables():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.delenv("REPORTS_BUCKET", raising=False)
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def bucket(monkeypatch):
    for name, value in (("AWS_ACCESS_KEY_ID", "testing"), ("AWS_SECRET_ACCESS_KEY", "testing"),
                        ("AWS_DEFAULT_REGION", "us-east-1"), ("REPORTS_BUCKET", "reports-test")):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        import boto3

        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="reports-test")
        yield s3


def case(case_id, *, status="done", created=NOV_23, customer_id="c_010"):
    return Case(case_id=case_id, customer_id=customer_id, status=status, created_at=created, updated_at=created)


def review(case_id, *, closed=NOV_23, outcome="done", recommendation="keep_limit", action=None, decisions=None,
           source="case", check_passed=True, customer_id="c_010"):
    return Review(
        case_id=case_id, customer_id=customer_id, as_of=datetime(2026, 11, 10).date(), closed_at=closed,
        outcome=outcome, source=source, check_passed=check_passed, likely_cause="payroll_or_tax",
        recommendation=recommendation, confidence="high", human_decision=None, guideline_ids=["G-08b"],
        review_reasons=[], policy_version="2026-11-18", current_limit_cents=1_000_000, engine_limit_cents=600_000,
        cash_7d_ago_cents=0, cash_now_cents=0, report={}, evidence=[],
        decisions=decisions if decisions is not None else [{"step": "analyst", "decision": "approve", "by": "u_analyst"}],
        action=action, asked=[], text="",
    )


def add(*rows):
    with SessionLocal() as session:
        session.add_all([row for row in rows if isinstance(row, Case)])
        session.flush()
        session.add_all([row for row in rows if isinstance(row, Review)])
        session.commit()


def november():
    """A month with every kind of case, and some that must not count."""
    approved_by_both = [{"step": "analyst", "decision": "approve", "by": "u_analyst"},
                        {"step": "manager", "decision": "approve", "by": "u_manager"}]
    add(
        case("case_cut"), review("case_cut", recommendation="reduce_limit", decisions=approved_by_both,
                                 action={"type": "reduce_limit", "limit_cents": 600_000, "result": {}}),
        case("case_pause"), review("case_pause", recommendation="pause_card", outcome="rejected",
                                   decisions=[{"step": "analyst", "decision": "reject", "by": "u_analyst"}]),
        case("case_human"), review("case_human", recommendation="needs_human", check_passed=False,
                                   decisions=[{"step": "analyst", "decision": "approve", "by": "u_analyst",
                                               "decided": "request_documents"}]),
        case("case_failed", status="failed"),  # failed before a checked report: no review
        case("case_open", status="needs_approval"),
        # Not November's: closed in October, and a seeded review.
        case("case_oct", created=datetime(2026, 10, 30, tzinfo=UTC)),
        review("case_oct", closed=datetime(2026, 10, 31, 23, 59, tzinfo=UTC)),
        # The seeder makes its case when it runs, so it is dated this month too.
        case("seed_p1"), review("seed_p1", source="seed"),
    )


def text(pdf: bytes) -> str:
    return "\n".join(page.extract_text() for page in pypdf.PdfReader(io.BytesIO(pdf)).pages)


def test_a_month_is_a_utc_calendar_month_and_december_runs_into_january():
    assert reports.month_range("2026-12") == (datetime(2026, 12, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC))
    assert reports.previous_month(datetime(2027, 1, 1, tzinfo=UTC)) == "2026-12"
    assert reports.previous_month(NOV_23) == "2026-10"


@pytest.mark.parametrize("month", ["2026-13", "2026-1", "26-11", "november", ""])
def test_a_month_not_written_yyyy_mm_is_refused(month):
    with pytest.raises(reports.BadMonth):
        reports.month_range(month)


def test_the_figures_count_this_months_cases_only_and_never_the_seeded_ones():
    november()
    with SessionLocal() as session:
        built = reports.build(session, "2026-11", NOV_23)
    totals = built["totals"]
    assert [row["case_id"] for row in built["rows"]] == ["case_cut", "case_human", "case_pause"]
    assert totals["opened"] == 5 and totals["closed"] == 3
    assert totals["still_open"] == 1 and totals["failed_before_a_report"] == 1
    assert totals["outcomes"] == {"done": 2, "rejected": 1}
    assert totals["report_check_failed"] == 1
    assert totals["actions"] == {"limit cut": 1, "no change": 2}
    assert totals["limit_removed_cents"] == 400_000
    assert totals["people"] == {"approved as recommended": 1, "rejected": 1, "handed to a person": 1}
    assert built["month_to_date"] is True
    by_case = {row["case_id"]: row["people"] for row in built["rows"]}
    assert by_case == {"case_cut": "u_analyst, u_manager", "case_pause": "rejected by u_analyst",
                       "case_human": "u_analyst; decided request_documents"}


def test_an_action_chosen_on_the_customers_documents_is_not_counted_as_approved_as_recommended():
    asked = [{"step": "analyst", "decision": "approve", "by": "u_analyst"},
             {"step": "documents", "decision": "approve", "by": "u_analyst", "decided": "pause_card"}]
    add(
        case("case_shown"), review("case_shown", recommendation="request_documents",
                                   decisions=[*asked, {"step": "manager", "decision": "approve", "by": "u_manager"}],
                                   action={"type": "pause_card", "result": {}}),
        case("case_refused", customer_id="c_011"),
        review("case_refused", recommendation="request_documents", outcome="rejected", customer_id="c_011",
               decisions=[*asked, {"step": "manager", "decision": "reject", "by": "u_manager"}]),
    )
    with SessionLocal() as session:
        people = reports.build(session, "2026-11", NOV_23)["totals"]["people"]

    assert people == {"decided on the documents": 1, "rejected": 1}


def test_the_pdf_names_every_closed_case_and_the_month(folder):
    november()
    made = reports.generate("2026-11", by="u_manager", now=NOV_23)
    pdf = (folder / made["key"]).read_bytes()
    page = text(pdf)
    for shown in ("case_cut", "case_pause", "case_human", "November 2026 (month to date)", "u_manager",
                  "$4,000 of limit removed"):
        assert shown in page
    assert "case_oct" not in page and "seed_p1" not in page
    assert made["sha256"] == hashlib.sha256(pdf).hexdigest()


def test_the_pdf_is_pdf_a_3u_with_its_fonts_embedded_and_its_figures_attached(folder):
    november()
    made = reports.generate("2026-11", by="u_manager", now=NOV_23)
    reader = pypdf.PdfReader(folder / made["key"])
    xmp = reader.trailer["/Root"]["/Metadata"].get_object().get_data()
    assert b"<pdfaid:part>3</pdfaid:part>" in xmp and b"<pdfaid:conformance>U</pdfaid:conformance>" in xmp
    fonts = reader.pages[0]["/Resources"]["/Font"].values()
    assert fonts and all(font["/DescendantFonts"][0].get_object()["/FontDescriptor"].get("/FontFile2")
                         for font in fonts)
    attached = json.loads(reader.attachments["report.json"][0])
    assert attached["totals"] == made["totals"]
    assert [row["case_id"] for row in attached["rows"]] == ["case_cut", "case_human", "case_pause"]
    assert attached["generated"] == {"at": "2026-11-23T14:05:00+00:00", "by": "u_manager", "how": "manual"}


def test_a_month_with_no_closed_case_still_gets_a_report_that_says_so(folder):
    made = reports.generate("2026-10", by="u_manager", now=NOV_23)
    assert "No case was closed in this month." in text((folder / made["key"]).read_bytes())
    assert made["month_to_date"] is False and made["totals"]["closed"] == 0


def test_a_copy_is_never_overwritten_and_running_again_adds_one(folder):
    first = reports.generate("2026-10", by="u_manager", now=NOV_23)
    second = reports.generate("2026-10", by="u_manager", now=NOV_23.replace(second=1))
    assert first["key"] != second["key"]
    assert len(list((folder / "monthly" / "2026-10").glob("*.pdf"))) == 2
    with pytest.raises(reports.AlreadyStored):
        reports.generate("2026-10", by="u_manager", now=NOV_23)


def test_a_month_that_has_not_begun_is_refused(folder):
    with pytest.raises(reports.BadMonth):
        reports.generate("2026-12", by="u_manager", now=NOV_23)


def test_with_no_storage_set_no_report_is_made(monkeypatch):
    monkeypatch.delenv("REPORTS_BUCKET", raising=False)
    monkeypatch.delenv("REPORTS_DIR", raising=False)
    with pytest.raises(reports.NoStorage):
        reports.generate("2026-10", by="u_manager", now=NOV_23)


def test_in_s3_the_copy_carries_its_sha256_and_is_never_overwritten(bucket):
    november()
    made = reports.generate("2026-11", by="u_manager", now=NOV_23)
    assert made["key"] == "monthly/2026-11/credit-review-2026-11-20261123T140500Z-manual.pdf"
    assert made["location"] == f"s3://reports-test/{made['key']}"
    # The region's own endpoint, which a new bucket outside us-east-1 needs for its download links.
    assert made["url"].startswith("https://reports-test.s3.us-east-1.amazonaws.com/")
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in made["url"] and "X-Amz-Expires=900" in made["url"]
    stored = bucket.get_object(Bucket="reports-test", Key=made["key"])
    body = stored["Body"].read()
    assert stored["ContentType"] == "application/pdf"
    assert stored["Metadata"] == {"sha256": hashlib.sha256(body).hexdigest(), "generated-by": "u_manager",
                                  "format": "PDF/A-3U"}
    with pytest.raises(reports.AlreadyStored):
        reports.generate("2026-11", by="u_manager", now=NOV_23)


class FakeBlob:
    def __init__(self, store, name):
        self.store, self.name, self.metadata = store, name, None

    def upload_from_string(self, data, *, content_type, if_generation_match):
        from google.api_core.exceptions import PreconditionFailed

        if if_generation_match == 0 and self.name in self.store:
            raise PreconditionFailed("an object has this name")
        self.store[self.name] = {"body": data, "content_type": content_type, "metadata": dict(self.metadata)}

    def generate_signed_url(self, **options):
        self.signed_with = options
        return f"https://storage.googleapis.com/reports-test/{self.name}?X-Goog-Algorithm=GOOG4-RSA-SHA256"


class FakeGcs:
    """Just what app/reports.py asks of google.cloud.storage.Client."""

    def __init__(self):
        self.objects, self.blobs = {}, []

    def bucket(self, name):
        assert name == "reports-test"
        return self

    def blob(self, name):
        self.blobs.append(FakeBlob(self.objects, name))
        return self.blobs[-1]

    def list_blobs(self, name, *, prefix, max_results):
        assert name == "reports-test"
        return [key for key in self.objects if key.startswith(prefix)][:max_results]


class VmCredentials:
    """What google.auth.default() gives on a VM: a service account with no key, whose email reads "default" until
    the first refresh."""

    service_account_email, token, valid = "default", None, False

    def refresh(self, request):
        self.service_account_email = "credit-review-k3s@project.iam.gserviceaccount.com"
        self.token, self.valid = "vm-token", True


@pytest.fixture
def gcs(monkeypatch):
    import google.auth

    monkeypatch.delenv("REPORTS_DIR", raising=False)
    monkeypatch.setenv("REPORTS_BUCKET", "gs://reports-test")
    fake = FakeGcs()
    monkeypatch.setattr(reports, "_gcs", lambda: fake)
    monkeypatch.setattr(google.auth, "default", lambda: (VmCredentials(), "project"))
    return fake


def test_in_cloud_storage_the_copy_carries_its_sha256_and_is_never_overwritten(gcs):
    november()
    made = reports.generate("2026-11", by="u_manager", now=NOV_23)
    assert made["location"] == f"gs://reports-test/{made['key']}"
    kept = gcs.objects[made["key"]]
    assert kept["content_type"] == "application/pdf"
    assert kept["metadata"] == {"sha256": hashlib.sha256(kept["body"]).hexdigest(), "generated-by": "u_manager",
                                "format": "PDF/A-3U"}
    assert made["sha256"] == kept["metadata"]["sha256"]
    # The VM's account has no key: the link is signed through IAM with its token, for 15 minutes.
    signed = gcs.blobs[0].signed_with
    assert signed["service_account_email"] == "credit-review-k3s@project.iam.gserviceaccount.com"
    assert signed["access_token"] == "vm-token" and signed["version"] == "v4"
    assert signed["expiration"].total_seconds() == 900 and made["url"].startswith("https://storage.googleapis.com/")
    with pytest.raises(reports.AlreadyStored):
        reports.generate("2026-11", by="u_manager", now=NOV_23)


def test_in_cloud_storage_a_link_that_cannot_be_signed_leaves_the_copy_kept(gcs, monkeypatch):
    def refused(self, **options):
        raise RuntimeError("signBlob refused")

    monkeypatch.setattr(FakeBlob, "generate_signed_url", refused)
    made = reports.generate("2026-11", by="u_manager", now=NOV_23)
    assert made["url"] is None and made["key"] in gcs.objects


def test_in_cloud_storage_the_schedule_makes_last_months_report_once(gcs):
    november()
    assert reports.run_due(DEC_2)["key"].endswith("-scheduled.pdf")
    assert reports.run_due(DEC_2.replace(hour=1)) is None
    assert len(gcs.objects) == 1


def test_the_schedule_makes_last_months_report_once(bucket):
    november()
    made = reports.run_due(DEC_2)
    assert made["month"] == "2026-11" and made["generated_by"] == "scheduler"
    assert made["key"].endswith("-scheduled.pdf") and made["totals"]["closed"] == 3
    assert reports.run_due(DEC_2.replace(hour=1)) is None
    assert bucket.list_objects_v2(Bucket="reports-test", Prefix="monthly/2026-11/")["KeyCount"] == 1


def test_a_service_that_starts_with_a_folder_set_catches_up_on_last_month(folder):
    with TestClient(app):
        pass
    month = reports.previous_month(datetime.now(UTC))
    assert len(list((folder / "monthly" / month).glob("*-scheduled.pdf"))) == 1


def test_a_service_with_no_storage_set_makes_no_report_on_its_own(tmp_path, monkeypatch):
    monkeypatch.delenv("REPORTS_DIR", raising=False)
    with TestClient(app):
        pass
    assert not list(tmp_path.iterdir())


@pytest.fixture
def api():
    with TestClient(app) as test_client:
        yield test_client


def test_a_manager_asks_for_a_month_and_gets_where_it_is_kept(folder, api):
    november()
    answer = api.post("/reports/monthly/2026-11", headers=bearer(sub="u_manager", role="manager"))
    assert answer.status_code == 201, answer.text
    body = answer.json()
    assert body["generated_by"] == "u_manager" and body["totals"]["closed"] == 3 and body["url"] is None
    assert (folder / body["key"]).read_bytes().startswith(b"%PDF")


@pytest.mark.parametrize("role", ["analyst", "service"])
def test_only_a_manager_or_an_admin_may_ask_for_a_report(folder, api, role):
    answer = api.post("/reports/monthly/2026-01", headers=bearer(role=role))
    assert answer.status_code == 403
    assert not (folder / "monthly" / "2026-01").exists()


@pytest.mark.parametrize("month", ["2026-13", "2999-01"])
def test_a_bad_or_future_month_is_refused(folder, api, month):
    answer = api.post(f"/reports/monthly/{month}", headers=bearer(sub="u_admin", role="admin"))
    assert answer.status_code == 422 and answer.json()["error"] == "invalid_request"


def test_with_no_storage_set_the_api_says_so(api, monkeypatch):
    monkeypatch.delenv("REPORTS_DIR", raising=False)
    answer = api.post("/reports/monthly/2026-10", headers=bearer(sub="u_manager", role="manager"))
    assert answer.status_code == 503
