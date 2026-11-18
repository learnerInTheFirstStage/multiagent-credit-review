"""Seed the Card database from data/shared_customers.yaml (or several files, see load()).

Idempotent: seed_if_empty(session) only runs when the customers table is empty.
Deterministic: the same file always gives the same rows and the same ids.

What the file fixes, and the seed reproduces exactly (snapshot.card, the "3. Scenario
table" numbers):
  - credit_limit_cents;
  - the balance on signal_date: round(credit_limit_cents * utilization_pct), so the
    utilization a tool works out from the rows is the file's to the basis point;
  - last_3_repayments: the statements due on the 5th of the last three months, in order;
  - the amounts of the last three repayments, by the rule the evaluation's signal uses
    (eval/signals.py): monthly_card_spend_cents, except that the latest one, when late or
    failed, has its event's amount from signal_window.card_events;
  - c_006's paying account (data_integrity): the first of the last three repayments
    comes from acct_c006_a, the last two from acct_external_new_c006.

What the seed adds on the card side, which the file leaves to each seeder: merchants,
cards, MCCs, the day-to-day spend, subscriptions, and the odd declined authorization.

How the ledger works:
  - A statement covers the 11th of one month to the 10th of the next. It is due on the
    5th of the month after it closes, by ACH autopay. So on 2026-11-10 the open cycle is
    2026-10-11 to 2026-11-10, and the statements due Sep 5, Oct 5 and Nov 5 are the last
    three repayments.
  - The balance is everything authorized and not declined, less every completed
    repayment. A failed (RETURNED) repayment leaves its statement owed, unless its event
    says repaid_after_days: then a second row for the statement, rep_{cid}_{YYYYMM}_r1,
    pays it by hand that many days after the return. A late one is paid by hand three
    days after the due date.
  - Each closed cycle before the last three spends the customer's monthly_card_spend_cents,
    a few percent either way; the last three spend exactly their repayment's amount.
  - A statement that is still owed sits on top of the next cycle's spend, so a monthly
    spend above HISTORY_CAP_SHARE of the limit could take the balance past it. Such a
    customer is an error, not quietly spent at less (see the README).
  - The open cycle's spend is whatever makes the signal-date balance match the file.
    Where the file gives a utilization_trend (c_003 rising, c_010 spiking), that spend is
    weighted towards the end of the cycle.
  - For those two, on each day of the open cycle the balance ends at 90% of the limit or
    more, one more authorization is attempted and declined AUTHORIZER_BUSINESS_LIMIT. Other
    customers can come near their limit too (c_004 does), but no story asks for a decline.
"""

from __future__ import annotations

import hashlib
import math
import os
import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import yaml
from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.db.models import CardTransaction, CreditLimit, Customer, Repayment

CYCLE_START_DAY = 11  # a cycle runs from the 11th to the 10th of the next month
DUE_DAY = 5  # due on the 5th of the month after the cycle closes
HISTORY_CYCLES = 14  # closed cycles before the open one: more than 400 days of history
HISTORY_CAP_SHARE = 0.45  # most monthly_card_spend_cents may be, as a share of the limit
LATE_DAYS = 3  # a late repayment is paid this many days after its due date
RETURN_DAYS = 2  # a failed ACH debit comes back this many days after it was sent
NEAR_LIMIT_SHARE = 0.9  # at this share of the limit, a further authorization is declined
NOISE_DECLINE_RATE = 0.006
SPEND_JITTER = 0.03
SIGMA = 0.3  # spread of one purchase's amount around the vendor's mean

ACH_RETURNS = {"R01": "Insufficient funds", "R02": "Account closed"}
NOISE_DECLINES = ("AUTHORIZER_CARD_MCC_BLACKLIST", "SUSPECTED_FRAUD", "WRONG_CVV")


def _default_yaml_paths() -> list[Path]:
    """Where the seeder looks when no path is passed.

    Order: SHARED_CUSTOMERS_YAML env var (one path, or several joined by ':', e.g. the team's
    file and eval/extra_customers.yaml), then /app/data/..., then the repo's data/ directory
    relative to this file (for running the tests locally).
    """
    env = os.environ.get("SHARED_CUSTOMERS_YAML")
    if env:
        return _split(env)
    in_container = Path("/app/data/shared_customers.yaml")
    if in_container.exists():
        return [in_container]
    # services/card/app/db/seed.py -> the repo root is parents[4]
    return [Path(__file__).resolve().parents[4] / "data" / "shared_customers.yaml"]


def _split(paths: str) -> list[Path]:
    return [Path(p) for p in paths.split(":") if p]


# ---------------------------------------------------------------------------
# Merchants: descriptors and ISO 18245 MCCs in common Visa/Mastercard wording.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Merchant:
    descriptor: str
    mcc: str
    mcc_description: str
    category: str  # the issuer's spend category, as finance teams see it
    city: str | None
    state: str | None
    country: str = "US"
    card_present: float = 0.0  # chance the card was physically present


MERCHANTS: dict[str, Merchant] = {
    "Delta Air Lines": Merchant("DELTA AIR LINES ATLANTA", "3058", "Delta Air Lines", "Airlines", "ATLANTA", "GA"),
    "Marriott": Merchant("MARRIOTT HOTELS", "7011", "Hotels, Motels, and Resorts", "Lodging", "NEW YORK", "NY", card_present=0.9),
    "Uber": Merchant("UBER *TRIP HELP.UBER.COM", "4121", "Taxicabs and Rideshares", "Ground transportation", "SAN FRANCISCO", "CA"),
    "Staples": Merchant("STAPLES 00115", "5943", "Stationery, Office, and School Supply Stores", "Office supplies", "BOSTON", "MA", card_present=0.6),
    "DoorDash": Merchant("DD *DOORDASH", "5812", "Eating Places and Restaurants", "Restaurants", "SAN FRANCISCO", "CA"),
    "LinkedIn Ads": Merchant("LINKEDIN ADS 855-6533", "7311", "Advertising Services", "Advertising", "SUNNYVALE", "CA"),
    "Meta Ads": Merchant("FACEBK *ADS", "7311", "Advertising Services", "Advertising", "MENLO PARK", "CA"),
    "Google Ads": Merchant("GOOGLE *ADS", "7311", "Advertising Services", "Advertising", "MOUNTAIN VIEW", "CA"),
    "Amazon Web Services": Merchant("AWS AMAZON WEB SERVICES", "4816", "Computer Network/Information Services", "Software & cloud", "SEATTLE", "WA"),
    "Google Workspace": Merchant("GOOGLE *GSUITE", "5734", "Computer Software Stores", "Software & cloud", "MOUNTAIN VIEW", "CA"),
    "Salesforce": Merchant("SALESFORCE.COM", "7372", "Computer Programming and Data Processing", "Software & cloud", "SAN FRANCISCO", "CA"),
    "Datadog": Merchant("DATADOG INC", "7372", "Computer Programming and Data Processing", "Software & cloud", "NEW YORK", "NY"),
    "Zoom": Merchant("ZOOM.US 888-799-9666", "7372", "Computer Programming and Data Processing", "Software & cloud", "SAN JOSE", "CA"),
    "Adobe": Merchant("ADOBE *CREATIVE CLD", "5734", "Computer Software Stores", "Software & cloud", "SAN JOSE", "CA"),
    "Shopify Plus": Merchant("SHOPIFY* PLUS", "5734", "Computer Software Stores", "Software & cloud", "OTTAWA", None, "CA"),
    "Toast": Merchant("TOAST INC", "5734", "Computer Software Stores", "Software & cloud", "BOSTON", "MA"),
    "WeWork": Merchant("WEWORK", "6513", "Real Estate Agents and Managers: Rentals", "Office rent", "NEW YORK", "NY"),
    "Getty Images": Merchant("GETTY IMAGES", "7333", "Commercial Photography, Art, and Graphics", "Creative services", "SEATTLE", "WA"),
    "B&H Photo": Merchant("B&H PHOTO 800-606-6969", "5732", "Electronics Stores", "Electronics", "NEW YORK", "NY"),
    "Digi-Key": Merchant("DIGI-KEY ELECTRONICS", "5065", "Electrical Parts and Equipment", "Electronic components", "THIEF RIVER FALLS", "MN"),
    "McMaster-Carr": Merchant("MCMASTER-CARR", "5085", "Industrial Supplies", "Industrial supplies", "ELMHURST", "IL"),
    "Grainger": Merchant("GRAINGER 8234", "5085", "Industrial Supplies", "Industrial supplies", "DENVER", "CO", card_present=0.4),
    "Uline": Merchant("ULINE *SHIP SUPPLIES", "5085", "Industrial Supplies", "Packaging & supplies", "PLEASANT PRAIRIE", "WI"),
    "ShipBob": Merchant("SHIPBOB INC", "4215", "Courier Services", "Shipping & logistics", "CHICAGO", "IL"),
    "FedEx": Merchant("FEDEX 78412339", "4215", "Courier Services", "Shipping & logistics", "MEMPHIS", "TN"),
    "Henry Schein": Merchant("HENRY SCHEIN INC", "5047", "Medical, Dental, Ophthalmic and Hospital Equipment", "Medical supplies", "MELVILLE", "NY"),
    "Sysco": Merchant("SYSCO METRO NY", "5499", "Miscellaneous Food Stores", "Food & restaurant supplies", "JERSEY CITY", "NJ"),
    "US Foods": Merchant("US FOODS INC", "5499", "Miscellaneous Food Stores", "Food & restaurant supplies", "PERTH AMBOY", "NJ"),
    "Restaurant Depot": Merchant("RESTAURANT DEPOT #12", "5300", "Wholesale Clubs", "Food & restaurant supplies", "BROOKLYN", "NY", card_present=1.0),
    "Brewers Supply Group": Merchant("BSG CRAFTBREWING", "5499", "Miscellaneous Food Stores", "Brewing supplies", "SHAKOPEE", "MN"),
    "ConEd": Merchant("CON ED OF NY", "4900", "Utilities: Electric, Gas, Water, Sanitary", "Utilities", "NEW YORK", "NY"),
    "Veritiv": Merchant("VERITIV OPERATING CO", "5111", "Stationery, Office Supplies, Printing and Writing Paper", "Paper & print supplies", "ATLANTA", "GA"),
    "Pilot Flying J": Merchant("PILOT_00351", "5542", "Automated Fuel Dispensers", "Fuel", "KNOXVILLE", "TN", card_present=1.0),
    "Love's Travel Stops": Merchant("LOVES #0456 OUTSIDE", "5542", "Automated Fuel Dispensers", "Fuel", "OKLAHOMA CITY", "OK", card_present=1.0),
    "TA Truck Service": Merchant("TA TRUCK SERVICE 0234", "7538", "Automotive Service Shops (Non-Dealer)", "Vehicle maintenance", "TOLEDO", "OH", card_present=1.0),
    "Shell": Merchant("SHELL OIL 57444", "5542", "Automated Fuel Dispensers", "Fuel", "DENVER", "CO", card_present=1.0),
    "REI Co-op": Merchant("REI #0042 SEATTLE", "5941", "Sporting Goods Stores", "Retail inventory", "SEATTLE", "WA", card_present=0.3),
}
# One Marriott stay in ten is in Paris, billed in euros.
PARIS_HOTEL = Merchant("MARRIOTT PARIS OPERA", "7011", "Hotels, Motels, and Resorts", "Lodging", "PARIS", None, "FR", card_present=1.0)
EUR_PER_USD = 0.92

DEPARTMENTS = {
    "Founders card": "Executive", "Owner card": "Executive", "Ops card": "Operations",
    "Sales card": "Sales", "Travel card": "Sales", "Marketing card": "Marketing",
    "Office card": "Finance", "Eng card": "Engineering", "Studio card": "Creative",
    "Fleet card A": "Fleet", "Fleet card B": "Fleet", "Kitchen card": "Production",
    "Taproom card": "Front of house", "Warehouse card": "Operations", "Tutor card": "Teaching",
    "Press room card": "Production",
}


# ---------------------------------------------------------------------------
# The card side of each customer: what the business buys on its cards. The file
# leaves this to the Card seeder; the totals above come from the file.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Vendor:
    name: str
    mean_cents: int
    weight: float


@dataclass(frozen=True)
class Recurring:
    day: int  # day of the month it bills
    name: str
    amount_cents: int


@dataclass(frozen=True)
class Profile:
    industry: str
    naics: str
    onboarded_on: date
    cards: tuple[str, ...]
    vendors: tuple[Vendor, ...]
    recurring: tuple[Recurring, ...] = ()
    memos: dict[str, str] = field(default_factory=dict)  # vendor -> an occasional benign memo


_TRAVEL = (Vendor("Delta Air Lines", 65_000, 2), Vendor("Marriott", 42_000, 2), Vendor("Uber", 3_800, 5))

PROFILES: dict[str, Profile] = {
    "c_001": Profile(
        "robotics", "333249", date(2022, 4, 1), ("Founders card", "Ops card", "Eng card"),
        (Vendor("Digi-Key", 120_000, 5), Vendor("McMaster-Carr", 80_000, 4), *_TRAVEL, Vendor("DoorDash", 9_500, 3)),
        (Recurring(1, "Amazon Web Services", 450_000), Recurring(3, "Google Workspace", 60_000)),
        {"Digi-Key": "Prototype parts", "DoorDash": "Team lunch"},
    ),
    "c_002": Profile(
        "software", "541511", date(2021, 9, 15), ("Founders card", "Sales card", "Marketing card"),
        (*_TRAVEL, Vendor("LinkedIn Ads", 180_000, 3), Vendor("DoorDash", 9_500, 4), Vendor("Staples", 15_000, 1)),
        (Recurring(1, "Amazon Web Services", 900_000), Recurring(4, "Salesforce", 300_000), Recurring(6, "Datadog", 150_000)),
        {"Delta Air Lines": "Customer visit"},
    ),
    "c_003": Profile(
        "freight_logistics", "488510", date(2021, 3, 1), ("Office card", "Fleet card A", "Fleet card B"),
        (Vendor("Pilot Flying J", 85_000, 7), Vendor("Love's Travel Stops", 70_000, 5), Vendor("TA Truck Service", 140_000, 3), Vendor("Staples", 15_000, 1)),
        (Recurring(2, "Amazon Web Services", 250_000),),
    ),
    "c_004": Profile(
        "design_studio", "541430", date(2022, 1, 10), ("Studio card", "Owner card"),
        (Vendor("B&H Photo", 160_000, 3), Vendor("Getty Images", 35_000, 3), Vendor("Meta Ads", 120_000, 3), Vendor("Uber", 3_500, 3), Vendor("DoorDash", 8_000, 3)),
        (Recurring(5, "Adobe", 120_000),),
        {"Getty Images": "Client campaign"},
    ),
    "c_005": Profile(
        "dental_supply", "423450", date(2020, 6, 1), ("Office card", "Warehouse card"),
        (Vendor("Henry Schein", 150_000, 4), Vendor("Uline", 40_000, 3), Vendor("FedEx", 12_000, 5), Vendor("Staples", 12_000, 1)),
        (Recurring(3, "Google Workspace", 30_000),),
    ),
    "c_006": Profile(
        "meal_kits", "311991", date(2023, 2, 1), ("Ops card", "Marketing card"),
        (Vendor("Sysco", 90_000, 4), Vendor("US Foods", 75_000, 3), Vendor("Uline", 30_000, 3), Vendor("Meta Ads", 60_000, 2)),
        (Recurring(2, "Shopify Plus", 230_000),),
    ),
    "c_007": Profile(
        "outdoor_retail", "459110", date(2019, 8, 1), ("Owner card", "Ops card", "Marketing card"),
        (Vendor("REI Co-op", 180_000, 3), Vendor("ShipBob", 30_000, 4), Vendor("Uline", 25_000, 2), Vendor("Google Ads", 60_000, 3), Vendor("Meta Ads", 55_000, 3)),
        (Recurring(2, "Shopify Plus", 230_000),),
    ),
    "c_008": Profile(
        "trucking", "484121", date(2020, 5, 1), ("Fleet card A", "Fleet card B", "Office card"),
        (Vendor("Pilot Flying J", 85_000, 8), Vendor("Love's Travel Stops", 70_000, 6), Vendor("TA Truck Service", 120_000, 4), Vendor("Shell", 12_000, 3), Vendor("Staples", 15_000, 1)),
    ),
    "c_009": Profile(
        "tutoring", "611691", date(2021, 7, 1), ("Founders card", "Tutor card"),
        (Vendor("Staples", 12_000, 4), Vendor("Meta Ads", 35_000, 2), Vendor("DoorDash", 6_000, 3), Vendor("Uber", 2_800, 2)),
        (Recurring(3, "Google Workspace", 30_000), Recurring(8, "Zoom", 25_000)),
    ),
    "c_010": Profile(
        "brewery", "312120", date(2021, 4, 15), ("Owner card", "Kitchen card", "Taproom card"),
        (Vendor("Brewers Supply Group", 180_000, 5), Vendor("Sysco", 70_000, 3), Vendor("Restaurant Depot", 40_000, 4), Vendor("Uline", 25_000, 1)),
        (Recurring(12, "ConEd", 280_000), Recurring(2, "Toast", 40_000)),
    ),
    "c_011": Profile(
        "commercial_printing", "323111", date(2020, 10, 1), ("Owner card", "Press room card"),
        (Vendor("Veritiv", 90_000, 5), Vendor("Grainger", 40_000, 2), Vendor("Uline", 25_000, 2), Vendor("Staples", 10_000, 2)),
        (Recurring(12, "ConEd", 150_000), Recurring(5, "Adobe", 30_000)),
    ),
    "c_012": Profile(
        "legal_software", "541511", date(2022, 2, 1), ("Founders card", "Sales card", "Eng card"),
        (*_TRAVEL, Vendor("DoorDash", 9_000, 3), Vendor("LinkedIn Ads", 90_000, 2)),
        (Recurring(1, "Amazon Web Services", 600_000), Recurring(1, "WeWork", 450_000), Recurring(4, "Salesforce", 200_000)),
    ),
    # The evaluation's one customer of its own (eval/extra_customers.yaml), twin of no team customer.
    "c_013": Profile(
        "catering", "722320", date(2021, 6, 1), ("Owner card", "Kitchen card", "Ops card"),
        (Vendor("Sysco", 85_000, 4), Vendor("US Foods", 70_000, 3), Vendor("Restaurant Depot", 40_000, 4),
         Vendor("Uline", 25_000, 2)),
        (Recurring(2, "Toast", 40_000),),
    ),
}

# The evaluation's other customers (eval/extra_customers.yaml) are each a team customer with one fact
# changed, something irrelevant added, or the ids renamed, so each buys what its twin buys. The
# file names no twin, so the link is kept here; the file's comments give each one.
TWINS: dict[str, str] = {
    "c_102": "c_002", "c_103": "c_003", "c_106": "c_006", "c_107": "c_007", "c_110": "c_010",
    "c_111": "c_010", "c_201": "c_001", "c_203": "c_003", "c_208": "c_008", "c_301": "c_001",
    "c_303": "c_003", "c_304": "c_004", "c_307": "c_007", "c_314": "c_004",
}

# Any other customer: a plain small business. No subscriptions, so any monthly spend covers its
# recurring charges, and onboarded before the earliest history the seed writes.
FALLBACK_PROFILE = Profile(
    "unclassified", "999990", date(2020, 1, 1), ("Owner card", "Ops card"),
    (Vendor("Staples", 15_000, 3), Vendor("Uber", 3_500, 3), Vendor("DoorDash", 8_000, 3),
     Vendor("FedEx", 12_000, 2), Vendor("Google Ads", 60_000, 1)),
)


def profile_for(cid: str) -> Profile:
    """The customer's own profile, else its twin's, else the fallback."""
    return PROFILES.get(cid) or PROFILES.get(TWINS.get(cid, "")) or FALLBACK_PROFILE


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _digits(key: str, n: int = 4) -> str:
    """A stable run of digits for a key: card last-4s and merchant ids."""
    return str(int(hashlib.sha256(key.encode()).hexdigest(), 16))[-n:]


def _shift_month(d: date, months: int, day: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    return date(d.year + y, m + 1, day)


def _at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=UTC)


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=k) for k in range((end - start).days + 1)]


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 30:  # a normal approximation keeps busy customers fast
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    threshold, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= threshold:
            return k
        k += 1


def _weekday_factor(d: date) -> float:
    return 1.25 if d.weekday() < 5 else 0.375  # averages to 1.0 over a week


def cycles(as_of: date) -> tuple[list[tuple[date, date]], tuple[date, date]]:
    """The closed cycles, oldest first, and the open one that holds as_of."""
    if as_of.day >= CYCLE_START_DAY:
        open_start = as_of.replace(day=CYCLE_START_DAY)
    else:
        open_start = _shift_month(as_of, -1, CYCLE_START_DAY)
    closed = []
    for back in range(HISTORY_CYCLES, 0, -1):
        start = _shift_month(open_start, -back, CYCLE_START_DAY)
        end = _shift_month(start, 1, CYCLE_START_DAY) - timedelta(days=1)
        closed.append((start, end))
    open_end = _shift_month(open_start, 1, CYCLE_START_DAY) - timedelta(days=1)
    return closed, (open_start, open_end)


def due_date(cycle_end: date) -> date:
    return _shift_month(cycle_end, 1, DUE_DAY)


# ---------------------------------------------------------------------------
# One customer.
# ---------------------------------------------------------------------------


class _Ledger:
    """The rows for one customer, built in order."""

    def __init__(self, cid: str, profile: Profile, as_of: date):
        self.cid, self.profile, self.as_of = cid, profile, as_of
        self.rng = random.Random(f"card-story-{cid}")  # amounts and counts
        self.rx = random.Random(f"card-realism-{cid}")  # times, FX, noise declines
        self.cards = [
            {
                "card_id": f"crd_{cid}_{i}",
                "last4": _digits(f"card-{cid}-{i}"),
                "nickname": nick,
                "department": DEPARTMENTS.get(nick),
            }
            for i, nick in enumerate(profile.cards, start=1)
        ]
        n = len(self.cards)
        self.card_weights = [1.0] if n == 1 else [0.5] + [0.5 / (n - 1)] * (n - 1)
        self.recurring_names = {r.name for r in profile.recurring}
        self.txns: list[dict] = []

    def _row(self, day: datetime, card: dict, name: str, amount: int, *, declined: str | None = None,
             memo: str | None = None) -> dict:
        m = MERCHANTS[name]
        merchant_amount, merchant_currency = amount, "USD"
        if name == "Marriott" and self.rx.random() < 0.1:
            m = PARIS_HOTEL
            merchant_amount, merchant_currency = round(amount * EUR_PER_USD), "EUR"
        state, cleared = "DECLINED", None
        if declined is None:
            settle = day + timedelta(days=self.rx.choice([1, 1, 2]), hours=6)
            if settle.date() > self.as_of or day.date() >= self.as_of - timedelta(days=1):
                state = "PENDING"  # authorized, not settled yet
            else:
                state, cleared = "CLEARED", settle
        return {
            "customer_id": self.cid,
            "card_id": card["card_id"],
            "card_last4": card["last4"],
            "card_nickname": card["nickname"],
            "cardholder_department": card["department"],
            "state": state,
            "transaction_type": "purchase",
            "authorized_at": day,
            "cleared_at": cleared,
            "amount_cents": amount,
            "currency": "USD",
            "merchant_amount_cents": merchant_amount,
            "merchant_currency": merchant_currency,
            "merchant_id": "mer_" + _digits(f"merchant-{name}", 8),
            "merchant_name": name,
            "merchant_descriptor": m.descriptor,
            "mcc": m.mcc,
            "mcc_description": m.mcc_description,
            "spend_category": m.category,
            "merchant_city": m.city,
            "merchant_state": m.state,
            "merchant_country": m.country,
            "card_present": self.rx.random() < m.card_present,
            "is_recurring": name in self.recurring_names,
            "decline_reason": declined,
            "original_txn_id": None,
            "memo": memo,
        }

    def spend_cycle(self, start: date, end: date, target: int, *, back_loaded: bool = False) -> None:
        """Post purchases on days start..min(end, as_of) that add up to exactly `target`."""
        days = _days(start, min(end, self.as_of))
        recurring = [(d, r) for d in days for r in self.profile.recurring if d.day == r.day]
        everyday_target = target - sum(r.amount_cents for _, r in recurring)
        if everyday_target <= 0:
            raise ValueError(f"{self.cid}: cycle {start}..{end} target {target} does not cover its subscriptions")

        vendors = self.profile.vendors
        total_w = sum(v.weight for v in vendors)
        mean_txn = sum(v.weight * v.mean_cents for v in vendors) / total_w * math.exp(SIGMA**2 / 2)
        expected = everyday_target / mean_txn
        n = len(days)
        day_w = [_weekday_factor(d) * (((i + 1) / n) ** 2 if back_loaded else 1.0) for i, d in enumerate(days)]
        norm = sum(day_w)

        drafts: list[tuple[date, Vendor, int, float]] = []  # day, vendor, card index, raw amount
        for d, w in zip(days, day_w, strict=True):
            for _ in range(_poisson(self.rng, expected * w / norm)):
                v = self.rng.choices(vendors, weights=[x.weight for x in vendors])[0]
                card = self.rng.choices(range(len(self.cards)), weights=self.card_weights)[0]
                drafts.append((d, v, card, v.mean_cents * self.rng.lognormvariate(0, SIGMA)))
        if not drafts:
            drafts.append((days[-1], vendors[0], 0, float(vendors[0].mean_cents)))

        # Scale so the cycle lands on its target exactly; the rounding goes on the largest purchase.
        raw_total = sum(a for *_, a in drafts)
        amounts = [max(100, round(a * everyday_target / raw_total)) for *_, a in drafts]
        biggest = max(range(len(amounts)), key=amounts.__getitem__)
        amounts[biggest] += everyday_target - sum(amounts)

        for (d, v, card, _), amount in zip(drafts, amounts, strict=True):
            when = _at(d, 8) + timedelta(minutes=self.rx.randint(0, 11 * 60))
            memo = self.profile.memos.get(v.name) if self.rx.random() < 0.15 else None
            self.txns.append(self._row(when, self.cards[card], v.name, amount, memo=memo))
            if self.rx.random() < NOISE_DECLINE_RATE:
                attempt = round(v.mean_cents * self.rx.lognormvariate(0, SIGMA))
                self.txns.append(self._row(when + timedelta(minutes=self.rx.randint(5, 90)), self.cards[card],
                                           v.name, attempt, declined=self.rx.choice(NOISE_DECLINES)))
        for d, r in recurring:
            self.txns.append(self._row(_at(d, 3), self.cards[0], r.name, r.amount_cents))

    def decline_near_limit(self, limit: int, repayments: list[dict], start: date, decline_from: date) -> None:
        """One AUTHORIZER_BUSINESS_LIMIT decline on each day from decline_from that the balance
        ends at 90% of the limit or more. The balance is worked out from `start`."""
        spent: dict[date, int] = {}
        for t in self.txns:
            if t["state"] != "DECLINED":
                d = t["authorized_at"].date()
                spent[d] = spent.get(d, 0) + t["amount_cents"]
        paid: dict[date, int] = {}
        for r in repayments:
            if r["status"] == "COMPLETED":
                d = r["settled_at"].date()
                paid[d] = paid.get(d, 0) + r["amount_cents"]
        balance = 0
        for d in _days(start, self.as_of):
            balance += spent.get(d, 0) - paid.get(d, 0)
            if d >= decline_from and balance >= NEAR_LIMIT_SHARE * limit:
                v = self.rng.choices(self.profile.vendors, weights=[x.weight for x in self.profile.vendors])[0]
                attempt = limit - balance + self.rng.randint(50_000, 300_000)
                card = self.cards[self.rng.randrange(len(self.cards))]
                self.txns.append(self._row(_at(d, 17, self.rng.randint(0, 59)), card, v.name, attempt,
                                           declined="AUTHORIZER_BUSINESS_LIMIT"))

    def finish(self) -> list[dict]:
        """Ids in time order: ctx_c_001_00001 is the customer's first authorization."""
        self.txns.sort(key=lambda t: t["authorized_at"])  # stable: same-time rows keep their order
        for n, t in enumerate(self.txns, start=1):
            t["txn_id"] = f"ctx_{self.cid}_{n:05d}"
        return self.txns


def generate(customer: dict, as_of: date) -> dict[str, list[dict]]:
    """Every row for one customer of the shared file."""
    cid = customer["id"]
    profile = profile_for(cid)
    snapshot = customer["snapshot"]["card"]
    baseline = customer["baselines"]["card"]
    events = {e["kind"]: e for e in customer["signal_window"].get("card_events") or []}
    primary_account = customer["snapshot"]["bank"]["linked_accounts"][0]["id"]

    limit = int(snapshot["credit_limit_cents"])
    balance_target = round(limit * snapshot["utilization_pct"])
    monthly = int(baseline["monthly_card_spend_cents"])
    statuses: list[str] = list(snapshot["last_3_repayments"])

    if monthly > HISTORY_CAP_SHARE * limit:
        raise ValueError(
            f"{cid}: monthly_card_spend_cents {monthly} is more than {HISTORY_CAP_SHARE:.0%} of the "
            f"credit limit {limit} (at most {math.floor(HISTORY_CAP_SHARE * limit)}); a statement still "
            "owed on top of the next cycle's spend would take the balance past the limit")

    closed, (open_start, open_end) = cycles(as_of)
    if closed[0][0] < profile.onboarded_on:
        raise ValueError(f"{cid}: history starts before onboarding")
    n_closed = len(closed)
    status_of = ["paid"] * (n_closed - len(statuses)) + statuses
    # As eval/signals.py reads the file: an event's amount is the latest repayment's, when that
    # one's status is the event's. Every other repayment of the last three is the monthly spend.
    for kind in ("late_repayment", "failed_repayment"):
        if kind in events and f"{status_of[-1]}_repayment" != kind:
            raise ValueError(f"{cid}: a {kind} event must be about the latest repayment, which is {status_of[-1]}")
    trouble_index = n_closed - 1 if f"{status_of[-1]}_repayment" in events else None

    change = events.get("funding_account_change")
    account_of = [primary_account] * n_closed
    funding_since = profile.onboarded_on
    if change:
        # data_integrity c_006: the first of the last three comes from `from`, the last two from `to`.
        account_of = [change["from"]] * (n_closed - 2) + [change["to"]] * 2
        funding_since = due_date(closed[-3][1]) + timedelta(days=15)

    ledger = _Ledger(cid, profile, as_of)
    repayments: list[dict] = []
    still_owed = 0
    for i, (start, end) in enumerate(closed):
        status = status_of[i]
        event = events.get(f"{status}_repayment") if i == trouble_index else None
        # Drawn for every cycle, used or not, so that which amount a cycle takes moves no later draw.
        jittered = round(monthly * (1 + ledger.rng.uniform(-SPEND_JITTER, SPEND_JITTER)))
        if event:
            target = int(event["amount_cents"])
        elif i >= n_closed - len(statuses):
            target = monthly  # one of the last three: what the signal shows
        else:
            target = jittered
        ledger.spend_cycle(start, end, target)

        due = due_date(end)
        rep = {
            "repayment_id": f"rep_{cid}_{due:%Y%m}",
            "customer_id": cid,
            "statement_period_start": start,
            "statement_period_end": end,
            "statement_balance_cents": target,
            "due_date": due,
            "amount_cents": target,
            "payment_method": "ach_autopay",
            "funding_account_id": account_of[i],
            "status": "SCHEDULED",
            "ach_return_code": None,
            "return_reason": None,
            "initiated_at": None,
            "settled_at": None,
            "returned_at": None,
        }
        retry = None
        if status == "failed":
            returned_on = due + timedelta(days=RETURN_DAYS)
            rep |= {"status": "RETURNED", "ach_return_code": "R01", "return_reason": ACH_RETURNS["R01"],
                    "initiated_at": _at(due, 6), "returned_at": _at(returned_on, 15)}
            after = event.get("repaid_after_days") if event else None
            if after is not None and event.get("still_unpaid"):
                raise ValueError(f"{cid}: a failed repayment cannot be both still_unpaid and repaid_after_days")
            if after is None:
                still_owed += target
            else:
                # Paid by hand later, under its own id: rep_{cid}_{YYYYMM} is the scheduled debit.
                paid_on = returned_on + timedelta(days=int(after))
                if paid_on > as_of:
                    raise ValueError(f"{cid}: repaid on {paid_on}, after the signal date {as_of}")
                retry = rep | {"repayment_id": f"{rep['repayment_id']}_r1", "payment_method": "ach_manual",
                               "status": "COMPLETED", "ach_return_code": None, "return_reason": None,
                               "initiated_at": _at(paid_on, 6), "settled_at": _at(paid_on, 14), "returned_at": None}
        elif status == "late":
            paid_on = due + timedelta(days=LATE_DAYS)
            rep |= {"payment_method": "ach_manual", "initiated_at": _at(paid_on, 6)}
            if paid_on <= as_of:
                rep |= {"status": "COMPLETED", "settled_at": _at(paid_on, 14)}
            else:
                rep["status"] = "INITIATED"
                still_owed += target
        elif due <= as_of:
            rep |= {"status": "COMPLETED", "initiated_at": _at(due, 6), "settled_at": _at(due, 14)}
        repayments.append(rep)
        if retry:
            repayments.append(retry)

    # The open cycle carries whatever brings the balance to the file's figure.
    stressed = "utilization_trend" in baseline
    open_target = balance_target - still_owed
    # What is owed when the open cycle starts, and the repayments that settle during it.
    carried = (sum(t["amount_cents"] for t in ledger.txns if t["state"] != "DECLINED")
               - sum(r["amount_cents"] for r in repayments if r["settled_at"] and r["settled_at"].date() < open_start))
    settling = sorted((r["settled_at"].date(), r["amount_cents"]) for r in repayments
                      if r["settled_at"] and open_start <= r["settled_at"].date() <= as_of)

    def peak() -> int:
        spent: dict[date, int] = {}
        for t in ledger.txns:
            if t["state"] != "DECLINED" and t["authorized_at"].date() >= open_start:
                spent[t["authorized_at"].date()] = spent.get(t["authorized_at"].date(), 0) + t["amount_cents"]
        balance, top = carried, carried
        for d in _days(open_start, min(open_end, as_of)):
            balance += spent.get(d, 0) - sum(a for s, a in settling if s == d)
            top = max(top, balance)
        return top

    state = (len(ledger.txns), ledger.rng.getstate(), ledger.rx.getstate())
    ledger.spend_cycle(open_start, open_end, open_target, back_loaded=stressed)
    if peak() > limit and settling and settling[-1][0] > open_start:
        # A statement paid late in the cycle (late, or returned and paid since) is still owed on
        # the days before, and the draw above took the balance past the limit. Redraw: the spend
        # before the last such payment is capped so the balance stays within the limit, and the
        # rest lands from that day on.
        del ledger.txns[state[0]:]
        ledger.rng.setstate(state[1])
        ledger.rx.setstate(state[2])
        last = settling[-1][0]
        share = (last - open_start).days / ((min(open_end, as_of) - open_start).days + 1)
        before = min(round(open_target * share), limit - carried)
        ledger.spend_cycle(open_start, last - timedelta(days=1), before, back_loaded=stressed)
        ledger.spend_cycle(last, open_end, open_target - before, back_loaded=stressed)
    if stressed:
        ledger.decline_near_limit(limit, repayments, closed[0][0], open_start)

    customer_row = {
        "customer_id": cid,
        "name": customer["name"],
        "industry": profile.industry,
        "naics_code": profile.naics,
        "onboarded_on": profile.onboarded_on,
        "status": "active",
        "card_status": "active",
        "credit_limit_cents": limit,
        "funding_account_id": account_of[-1],
        "funding_account_since": funding_since,
        "autopay_enabled": True,
        "card_feed_updated_at": _at(as_of, 6),
        "payments_feed_updated_at": _at(as_of, 6),
    }
    limit_row = {
        "customer_id": cid,
        "effective_on": profile.onboarded_on,
        "limit_cents": limit,
        "reason": "initial underwriting",
        "approval_id": None,
    }
    return {
        "customers": [customer_row],
        "credit_limits": [limit_row],
        "card_transactions": ledger.finish(),
        "repayments": repayments,
    }


# ---------------------------------------------------------------------------
# Entry points.
# ---------------------------------------------------------------------------


def load(yaml_path: Path | str | None = None) -> dict:
    """The customers of one file, or of several (a str joined by ':'), as one file's shape.

    The files must share schema_version and clock, and no customer id may appear twice.
    """
    paths = _split(yaml_path) if isinstance(yaml_path, str) else [yaml_path] if yaml_path else _default_yaml_paths()
    merged: dict | None = None
    seen: dict[str, Path] = {}
    for path in paths:
        with open(path) as f:
            data = yaml.safe_load(f)
        if merged is None:
            merged = {**data, "customers": []}
        for key in ("schema_version", "clock"):
            if data.get(key) != merged.get(key):
                raise ValueError(f"{path}: {key} {data.get(key)!r} differs from {paths[0]}'s {merged.get(key)!r}")
        for customer in data["customers"]:
            cid = customer["id"]
            if cid in seen:
                raise ValueError(f"customer {cid} is in both {seen[cid]} and {path}")
            seen[cid] = path
            merged["customers"].append(customer)
    if merged is None:
        raise ValueError("no customer file given")
    return merged


def seed(session: Session, yaml_path: Path | str | None = None) -> dict[str, int]:
    """Populate every table. Flushes but does not commit. Returns row counts."""
    data = load(yaml_path)
    as_of = date.fromisoformat(data["clock"]["signal_date"])
    totals = {"customers": 0, "credit_limits": 0, "card_transactions": 0, "repayments": 0}
    for customer in data["customers"]:
        rows = generate(customer, as_of)
        # Parents first, for the foreign keys.
        for table, model in (("customers", Customer), ("credit_limits", CreditLimit),
                             ("card_transactions", CardTransaction), ("repayments", Repayment)):
            if rows[table]:
                session.execute(insert(model), rows[table])
                totals[table] += len(rows[table])
    session.flush()
    return totals


def is_empty(session: Session) -> bool:
    return session.query(Customer).first() is None


def seed_if_empty(session: Session, yaml_path: Path | str | None = None) -> dict[str, int] | None:
    """Run seed() only if customers is empty. Commits on success."""
    if not is_empty(session):
        return None
    summary = seed(session, yaml_path)
    session.commit()
    return summary


def main() -> None:
    """CLI: python -m app.db.seed"""
    from app.db import Base, SessionLocal, engine

    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        summary = seed_if_empty(session)
        print("database already seeded; no changes made" if summary is None else f"seeded: {summary}")


if __name__ == "__main__":
    main()
