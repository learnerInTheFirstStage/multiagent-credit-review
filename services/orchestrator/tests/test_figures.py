"""No figure a model made up (spec 6.1): the tokenizer, the allow-list and the check of a report."""

import time

import pytest

from app.graph import facts
from app.graph.figures import allowed_figures, figures, made_up, not_backed
from tests.test_facts import WINDOW, request, review, stand_in_records, walk
from tests.test_signals import SIGNAL  # c_010

# What the transactions topic returns for c_010 (spec section 3, step 2), built by hand.
BANK = [
    {
        "id": "bal_c_010",
        "kind": "daily_balance",
        "data": {"cash_week_ago_cents": 52000000, "cash_now_cents": 29000000, "change_cents": -23000000},
    },
    {
        "id": "txn_c_010_01",
        "kind": "bank_transaction",
        "data": {"amount_cents": -15000000, "category": "payroll", "share_of_fall_bp": 6522},
    },
    {
        "id": "txn_c_010_02",
        "kind": "bank_transaction",
        "data": {"amount_cents": -7000000, "category": "tax", "share_of_fall_bp": 3043},
    },
    {
        "id": "agg_c_010_payments",
        "kind": "payment_summary",
        "data": {
            "listed_count": 2,
            "listed_out_cents": 22000000,
            "listed_share_bp": 9565,
            "not_listed_cents": 1000000,
            "not_listed_share_bp": 435,
            "payroll_and_tax_cents": 22000000,
            "payroll_and_tax_share_bp": 9565,
        },
    },
    {
        "id": "agg_c_010_inflows",
        "kind": "weekly_inflow_series",
        "data": {"weekly_inflow_cents": 8000000, "income_trend": "steady", "weeks": 13},
    },
]
CASE = {record["id"]: record for record in [*facts.signal_facts(SIGNAL, WINDOW), *BANK]}
ALLOWED = allowed_figures(CASE.values())
PAYEE = {
    "id": "agg_c_011_payee_history",
    "kind": "payee_history",
    "data": {"n_prior_payments": 18, "first_seen_months_ago": 24, "typical_amount_cents": 4000000},
}


def report(**parts) -> dict:
    """A report that states nothing, with the parts given."""
    empty = {"evidence": [], "ruled_out": [], "other_findings": [], "review_reasons": [], "next_action": None}
    return {"summary": "Cash fell.", **empty, **parts}


def claim(text: str, *ids: str) -> dict:
    return {"claim": text, "evidence_ids": list(ids)}


def backed(text: str, *records: dict) -> bool:
    return not_backed(text, allowed_figures(records)) == []


# The tokenizer.


def test_a_figure_is_the_same_figure_however_it_is_written():
    fall = CASE["sig_cash"]  # shows "$230,000"

    money = ("$230,000", "$230K", "$0.23M", "$230 thousand", "230,000 dollars", "-$230,000", "$230,000.00")
    plain = ("230,000", "230k", "230 thousand", "230000")

    assert {token for text in money for token in figures(text)} == {"m:230000"}
    assert {token for text in plain for token in figures(text)} == {"n:230000"}
    for text in money + plain:
        assert backed(text, fall), text
    # Shares and cents are figures too.
    assert figures("96 percent, 96 %, 96.0% and $12.50") == {"p:96": "96 percent", "m:12.5": "$12.50"}


def test_a_figure_that_is_close_but_not_equal_is_another_figure():
    payments = CASE["agg_c_010_payments"]  # shows "96%" and "$220,000"

    assert not_backed("$229,000 and $230,001", allowed_figures([CASE["sig_cash"]])) == ["$229,000", "$230,001"]
    assert not_backed("95.65% of it", allowed_figures([payments])) == ["95.65%"]
    assert backed("96% of it, $220,000 in all", payments)
    assert not_backed("about $200,000", allowed_figures([payments])) == ["$200,000"]


def test_ids_that_hold_digits_are_not_figures():
    text = "G-07, G-08b, G-14, txn_c_010_01, acct_c010_a, c_010, hist_seed_c_007_2025, hist_case_1a2b3c4d, rep_c_110_3."

    assert figures(text) == {}
    # Not when they are set in emphasis either.
    assert figures("_hist_seed_c_007_2025_, __G-07__, _c_010_ and *txn_c_010_01*") == {}


def test_emphasis_or_a_missing_space_does_not_hide_a_figure():
    # An underscore is markdown once the ids are gone, and a full stop is a decimal point only after a digit.
    assert figures("Roughly _90,000_ is not accounted for.") == {"n:90000": "90,000"}
    assert figures("Payroll and tax are __96%__ of the fall.") == {"p:96": "96%"}
    assert figures("Cash fell.90,000 is unexplained.") == {"n:90000": "90,000"}
    assert figures("fell.96% was payroll") == {"p:96": "96%"}
    assert figures("Only .5% of it, $.50 each, and 2000.5 in all.") == {
        "p:0.5": ".5%",
        "m:0.5": "$.50",
        "n:2000.5": "2000.5",
    }
    assert figures("_230k_ on _2026-11-06_, _Nov 6_, the _18th_, _4x_ the usual, _half_ of it") == {
        "n:230000": "230k",
        "d:2026-11-06": "2026-11-06",
        "d:11-06": "Nov 6",
        "n:18": "18th",
        "w:times": "4x",
        "w:half": "half",
    }
    # The pilot's sentence in emphasis is refused for every figure, as it is without.
    draft = report(
        summary="Inflows of about _$80,000_ make true outflows about _310,000_, so roughly _90,000_ is missing.",
        evidence=[claim("Records and balances do not match.", "bal_c_010")],
    )
    assert made_up(draft, CASE) == [
        ("summary", "$80,000", "agg_c_010_inflows"),
        ("summary", "310,000", None),
        ("summary", "90,000", None),
    ]


def test_a_date_counts_only_when_the_cited_record_holds_it():
    repayments, card = CASE["sig_repayments"], CASE["sig_card"]  # one is due 2026-11-05

    for text in ("The Nov 5 repayment failed.", "November 5th", "5 November 2026", "on 2026-11-05", "in 2026", "nov 5"):
        assert backed(text, repayments), text
    for text, written in (
        ("The Nov 6 repayment failed.", "Nov 6"),
        ("The 6 November repayment failed.", "6 November"),
        ("The nov 6 repayment failed.", "nov 6"),
        ("THE NOV 6 REPAYMENT FAILED.", "NOV 6"),
        ("due Nov 5, 2025", "Nov 5, 2025"),
    ):
        assert not_backed(text, allowed_figures([repayments])) == [written], text
    # Lower-case "may" is the verb.
    assert figures("2 may be late and 3 may fail, but May 5 was paid.") == {"d:05-05": "May 5"}
    found = made_up(report(evidence=[claim("The Nov 5 repayment failed.", "sig_card")]), CASE)
    assert found == [("evidence 1", "Nov 5", "sig_repayments")]
    assert not backed("Nov 5", card)


def test_a_year_or_a_date_is_not_read_out_of_an_amount():
    assert figures("$2000") == {"m:2000": "$2000"}
    assert figures("2000%") == {"p:2000": "2000%"}
    assert figures("2000k") == {"n:2000000": "2000k"}
    assert figures("2,000 and 2000.5") == {"n:2000": "2,000", "n:2000.5": "2000.5"}
    assert figures("2000 dollars") == {"m:2000": "2000 dollars"}
    assert figures("In Nov 50,000 went out") == {"n:50000": "50,000"}
    assert figures("in 2025, cash fell") == {"y:2025": "2025"}


def test_counts_up_to_twelve_pass_and_larger_ones_must_be_in_the_evidence():
    assert figures("The last 3 repayments, two accounts, 12 payments, the 3rd one, a dozen.") == {}
    assert figures("13 weeks, the 13th week, 0.5 of it") == {"n:13": "13", "n:0.5": "0.5"}
    assert figures("the 18th payment") == {"n:18": "18th"}
    assert not backed("over the last 13 weeks", CASE["sig_card"])
    assert backed("over the last 13 weeks", CASE["agg_c_010_inflows"])
    # A count of twelve with a scale or a sign is an amount.
    assert figures("$12, 12%, 12k") == {"m:12": "$12", "p:12": "12%", "n:12000": "12k"}


def test_paid_18_times_before_is_a_count_and_twice_in_six_months_states_nothing():
    sentence = "The payee was paid 18 times before over 24 months, usually $40,000."

    assert figures(sentence) == {"n:18": "18", "n:24": "24", "m:40000": "$40,000"}
    assert made_up(report(evidence=[claim(sentence, PAYEE["id"])]), {PAYEE["id"]: PAYEE}) == []
    assert [written for _, written, _ in made_up(report(evidence=[claim(sentence, "txn_c_010_01")]), CASE)] == [
        "18",
        "24",
        "$40,000",
    ]
    assert figures("The customer was asked twice in the last six months, and paid 2 times, the second late.") == {}


def test_a_dollar_figure_is_backed_only_by_a_money_value():
    inflows = CASE["agg_c_010_inflows"]  # weeks: 13

    assert not_backed("$13", allowed_figures([inflows])) == ["$13"]
    assert backed("13", inflows)
    assert backed("230,000", CASE["sig_cash"])
    # A bare number in a record backs the same number, never dollars.
    odd = {"id": "agg_odd", "kind": "payment_summary", "data": {"total": 23000000}}
    assert backed("23,000,000", odd)
    assert not_backed("$23,000,000", allowed_figures([odd])) == ["$23,000,000"]


@pytest.mark.parametrize(
    ("text", "written"),
    [
        ("4 times the usual", "4 times the"),
        ("twice as large", "twice as"),
        ("3x", "3x"),
        ("by a factor of 4", "factor of 4"),
        ("fourfold", "fourfold"),
        ("quadrupled", "quadrupled"),
        ("a 4-fold rise", "4-fold"),
        ("1.5x the payroll", "1.5x"),
        ("doubled", "doubled"),
        ("three times larger", "three times larger"),
        ("thrice as much", "thrice as"),
    ],
)
def test_a_multiple_the_model_worked_out_is_refused_even_when_it_is_small(text, written):
    # Every number in it is a count that would pass on its own, and every record is allowed.
    assert figures(text) == {"w:times": written}
    assert not_backed(text, ALLOWED | {"n:4", "n:3", "n:1.5", "n:2"}) == [written]


@pytest.mark.parametrize(
    ("text", "written"),
    [
        ("roughly ninety thousand", "ninety thousand"),
        ("half the fall", "half"),
        ("a quarter of it", "a quarter"),
        ("two thirds of the fall", "thirds"),
        ("a fifth of it", "a fifth of"),
        ("one-third", "one-third"),
        ("three quarters of the fall", "three quarters"),
        ("2 quarters of the fall", "2 quarters"),
        ("ninety percent", "ninety percent"),
        ("cash halved", "halved"),
        ("income halves", "halves"),
        ("two hundred thirty thousand dollars", "two hundred thirty thousand"),
        ("ninety thousand dollars", "ninety thousand"),
    ],
)
def test_a_size_written_in_words_is_refused(text, written):
    found = figures(text)

    assert written in found.values()
    assert all(token.startswith("w:") for token, as_written in found.items() if as_written == written)
    assert written in not_backed(text, ALLOWED)


@pytest.mark.parametrize(
    ("text", "written"),
    [
        ("Card spend rose x2 in the week.", "x2"),
        ("Card spend rose ×2 in the week.", "×2"),
        ("Card spend rose × 2 in the week.", "× 2"),
        ("x1.5 the payroll", "x1.5"),
        ("X3 the usual", "X3"),
        ("rose _x2_", "x2"),
    ],
)
def test_a_multiple_written_before_its_number_is_refused(text, written):
    # 0.3, item 12. Every number in it is a count that would pass on its own.
    assert figures(text) == {"w:times": written}
    assert not_backed(text, ALLOWED | {"n:2", "n:1.5", "n:3"}, loose=True) == [written]


def test_pct_is_a_percent():
    payments = CASE["agg_c_010_payments"]  # shows "96%"

    assert figures("44pct of the fall") == {"p:44": "44pct"}
    assert figures("44 pct, 44 PCT") == {"p:44": "44 pct"}
    assert backed("96pct of it", payments)
    assert not_backed("95pct of it", allowed_figures([payments])) == ["95pct"]
    # Not a year that loose mode would let through, and not a date.
    assert figures("2025 pct") == {"p:2025": "2025 pct"}
    assert not_backed("2025 pct of it", set(), loose=True) == ["2025 pct"]
    assert figures("May 20 pct") == {"p:20": "20 pct"}
    assert "w:pct" in figures("forty pct of it")


@pytest.mark.parametrize(
    ("text", "written"),
    [("$ 2000 went out", "$ 2000"), ("$-2000 went out", "$-2000"), ("$ -2000 went out", "$ -2000"),
     ("$ 5 May", "$ 5")],
)
def test_a_dollar_sign_and_a_space_make_dollars_never_a_year_or_a_date(text, written):
    found = figures(text)

    assert [token for token in found if not token.startswith("m:")] == []
    assert list(found.values()) == [written]
    assert not_backed(text, set(), loose=True) == [written]


@pytest.mark.parametrize(
    ("text", "token", "written"),
    [("$  2000 went out", "m:2000", "$  2000"), ("$\t 2026 went out", "m:2026", "$\t 2026"),
     ("$  5 May", "m:5", "$  5")],
)
def test_any_run_of_whitespace_after_a_dollar_sign_makes_dollars(text, token, written):
    assert figures(text) == {token: written}
    assert not_backed(text, set(), loose=True) == [written]
    # A next step reads money too.
    assert figures(text, marked_only=True) == {token: written}


@pytest.mark.parametrize(
    ("text", "written"),
    [
        ("Card spend rose ×  2 in the week.", "×  2"),
        ("Card spend rose ×2 May 2026.", "×2"),
        ("rose × 2025", "× 2025"),
    ],
)
def test_a_multiple_before_its_number_is_read_however_far_apart_and_whatever_follows(text, written):
    assert figures(text)["w:times"] == written
    assert not_backed(text, set(), loose=True) == [written]


def test_a_dollar_sign_before_a_percent_leaves_the_percent_as_it_was():
    assert figures("$50% and $ 96%") == {"p:50": "50%", "p:96": "96%"}


@pytest.mark.parametrize(
    "text",
    [
        "box2", "box 2", "fix2", "0x2F", "x-ray", "the x axis", "acct_x2_a", "txn_c_010_x2", "hist_case_x2b",
        "Payroll and tax went out in the window.", "The customer was asked twice, and the box was checked.",
        "The 3rd repayment failed on May 5.", "in 2025 the fall", "$2000 and 2000%",
    ],
)
def test_the_tighter_tokenizer_reads_no_more_in_ids_words_and_ordinary_text(text):
    before = {
        "in 2025 the fall": {"y:2025": "2025"},
        "The 3rd repayment failed on May 5.": {"d:05-05": "May 5"},
        "$2000 and 2000%": {"m:2000": "$2000", "p:2000": "2000%"},
    }

    assert figures(text) == before.get(text, {})


def test_a_long_run_of_number_words_is_read_in_time_that_grows_with_its_length_only():
    # A model stuck repeating itself must not stall the check: retrying every start made 8,000 words take seconds.
    started = time.perf_counter()
    found = [figures("one " * 8000), figures("one " * 8000 + "thousand")]

    assert time.perf_counter() - started < 1
    assert [list(each) for each in found] == [[], ["w:thousand"]]


def test_quarterly_and_halfway_are_not_sizes_and_neither_are_the_quarter_the_first_half_or_the_third_repayment():
    for text in (
        "quarterly",
        "halfway through the month",
        "the tax payment for the quarter",
        "last quarter",
        "a quarter ago",
        "the first half of the year",
        "the second half",
        "the third repayment",
        "a fifth payment",
        "the tax for the 3rd quarter",
        "on behalf of the customer",
        "the second-half results",
        # Ordinals past twelve, written as words.
        "the twenty-third repayment",
        "due on the twenty-fifth",
    ):
        assert figures(text) == {}, text


def test_figures_are_reported_in_the_order_written():
    found = figures("On 2026-11-05 $50,000 failed: 96% of 13 weeks, a third of the fall, 4x the usual.")

    assert list(found) == ["d:2026-11-05", "m:50000", "p:96", "n:13", "w:third", "w:times"]


def test_a_next_step_is_read_for_money_and_percent_only():
    assert figures("Within 14 days, by Nov 20 2026, twice a week, half of it", marked_only=True) == {}
    assert figures("Hold $58,000, 44% of it, or 58k, txn_c_010_01", marked_only=True) == {
        "m:58000": "$58,000",
        "p:44": "44%",
        "n:58000": "58k",
    }


# The allow-list.


def test_the_name_of_a_key_never_makes_a_figure_allowed():
    data = {"last_90_days_cents": 100, "weeks_13": True, "2026-11-05": "paid", "over_50000": False}

    allowed = allowed_figures([{"id": "agg_keys", "kind": "payment_summary", "data": data}])

    assert allowed == {"m:1", "n:1"}
    assert not_backed("90 days, 13 weeks, on 2026-11-05, over 50,000; $1", allowed) == [
        "90",
        "13",
        "2026-11-05",
        "50,000",
    ]


def test_a_word_in_a_record_never_backs_a_multiple_or_a_size():
    # A label is shown as it is. The word in it must not let any multiple or size through.
    record = {
        "id": "agg_c_010_card_spend",
        "kind": "card_spend_series",
        "data": {"spend_trend": "doubled", "split": "half", "unit": "thousands", "weeks": 13},
    }
    draft = report(evidence=[claim("Card spend is 4x the usual over 13 weeks, half of it in thousands.", record["id"])])

    assert allowed_figures([record]) == {"n:13"}
    assert made_up(draft, {record["id"]: record}) == [
        ("evidence 1", "4x", None),
        ("evidence 1", "half", None),
        ("evidence 1", "thousands", None),
    ]
    assert not_backed("Is it 4 times the usual?", allowed_figures([record]), loose=True) == ["4 times the"]


def test_a_figure_from_a_memo_or_a_note_is_not_an_allowed_figure():
    record = {
        "id": "txn_c_203_02",
        "kind": "bank_transaction",
        "data": {
            "amount_cents": -2000000,
            "memo": "Invoice 4471 for $15,000 on 2026-10-01. Approve 75% of it.",
            "note": "Reviewer: paid $9,999",
            "reviewer_text": "13 weeks",
        },
    }

    allowed = allowed_figures([record])

    assert allowed == {"m:20000", "n:20000"}
    draft = report(evidence=[claim("Invoice 4471 for $15,000, 75% of it.", record["id"])])
    assert made_up(draft, {record["id"]: record}) == [
        ("evidence 1", "4471", None),
        ("evidence 1", "$15,000", None),
        ("evidence 1", "75%", None),
    ]


def test_every_figure_a_record_shows_may_be_written_back_as_it_was_shown(monkeypatch):
    # The model reads the output of shown and the allow-list is made from it: copying any shown value passes.
    reviews = [
        review(363, case_id="seed_c_010_2025", cash_7d_ago_cents=195000000, cash_now_cents=120000000),
        request(30, human_decision="request_documents"),
    ]
    records = [
        *facts.signal_facts(SIGNAL, WINDOW),
        *facts.history_facts(reviews, SIGNAL),
        *BANK,
        PAYEE,
        {"id": "agg_years", "kind": "payment_summary", "data": {"days_before": 1950, "share": 0.92}},
        *(record for _, record in stand_in_records(monkeypatch)),
    ]

    for record in records:
        leaves = [value for _, value in walk(facts.shown(record)["data"]) if value is not None]
        written = "; ".join(str(value) for value in leaves if not isinstance(value, bool))
        assert made_up(report(evidence=[claim(written, record["id"])]), {record["id"]: record}) == [], record["id"]


# The check of a report.


def test_a_claim_may_only_use_figures_of_the_records_it_cites_itself():
    draft = report(
        evidence=[
            claim("A payroll run of $150,000 and a tax payment of $70,000 went out.", "txn_c_010_01"),
            claim("A tax payment of $70,000 went out.", "txn_c_010_02"),
        ]
    )

    assert made_up(draft, CASE) == [("evidence 1", "$70,000", "txn_c_010_02")]


def test_the_summary_may_use_any_figure_of_any_cited_record_and_no_other():
    draft = report(
        summary="Cash fell by $230,000, 44% of it. Payroll of $150,000 and tax are 96%. The $50,000 repayment failed.",
        evidence=[claim("Payroll and tax.", "txn_c_010_01", "agg_c_010_payments")],
        review_reasons=[{"reason": "large_cut", "finding": "Cash fell.", "evidence_ids": ["sig_cash"]}],
    )

    assert made_up(draft, CASE) == [("summary", "$50,000", "sig_repayments")]
    draft["review_reasons"].append({"reason": "failed_repayment", "finding": "Yes.", "evidence_ids": ["sig_repayments"]})
    assert made_up(draft, CASE) == []


def test_an_item_that_cites_nothing_may_state_no_figure():
    draft = report(
        ruled_out=[
            {"cause": "seasonal_dip", "reason": "Last year $90,000 went out in the same weeks.", "evidence_ids": []},
            {"cause": "revenue_loss", "reason": "Nothing shows income falling.", "evidence_ids": []},
        ],
        review_reasons=[{"reason": "failed_repayment", "finding": "Why is not determined.", "evidence_ids": []}],
    )

    assert made_up(draft, CASE) == [("ruled_out 1", "$90,000", None)]
    # Not even a figure that a record cited elsewhere in the report shows.
    draft = report(
        evidence=[claim("Cash fell by $230,000, 44% of it.", "sig_cash")],
        ruled_out=[{"cause": "revenue_loss", "reason": "Cash fell by $230,000, 44% of it.", "evidence_ids": []}],
    )
    assert made_up(draft, CASE) == [("ruled_out 1", "$230,000", "sig_cash"), ("ruled_out 1", "44%", "sig_cash")]


def test_a_refused_figure_that_another_record_of_the_case_holds_is_reported_with_that_record():
    draft = report(
        evidence=[
            claim("Only $220,000 of $230,000 is explained, so $10,000 is missing.", "txn_c_010_01", "txn_c_010_02")
        ],
        other_findings=[claim("The listed payments are 96% of the fall.", "agg_c_010_payments")],
    )

    # The records are named in ledger order, the case's own records first. A record cited in another part counts.
    assert made_up(draft, CASE) == [
        ("evidence 1", "$220,000", "agg_c_010_payments"),
        ("evidence 1", "$230,000", "sig_cash"),
        ("evidence 1", "$10,000", "agg_c_010_payments"),
    ]


def test_an_id_the_ledger_does_not_hold_backs_nothing_and_breaks_nothing():
    draft = report(summary="Cash fell by $230,000.", evidence=[claim("Cash fell by $230,000.", "bal_unknown")])

    assert made_up(draft, CASE) == [("summary", "$230,000", "sig_cash"), ("evidence 1", "$230,000", "sig_cash")]


def test_a_question_may_name_a_date_or_a_span_of_days_and_no_other_figure_the_ledger_lacks():
    for question in (
        "Which payments went out in the last 90 days?",
        "What went out in the same weeks of 2025?",
        "Was anything paid between 2026-10-01 and Nov 5?",
        "What happened over a 90-day window, and to the $230,000?",
    ):
        assert not_backed(question, ALLOWED, loose=True) == [], question
    for question, written in (
        ("Where did the other $90,000 go?", "$90,000"),
        ("Where did the other 90,000 go?", "90,000"),
        ("Where did the other ninety thousand go?", "ninety thousand"),
        ("Is it 4 times the usual?", "4 times the"),
        # A word that starts with a unit of time is no span.
        ("Was the 90,000 year-end payment planned?", "90,000"),
        ("Where did the 90,000 month-end transfer go?", "90,000"),
        ("Is 90,000 day-to-day spending?", "90,000"),
    ):
        assert not_backed(question, ALLOWED, loose=True) == [written], question
    # Outside a question a span is a figure like any other.
    assert not_backed("the last 90 days", ALLOWED) == ["90"]


def test_a_deadline_in_the_next_action_is_not_a_figure_and_a_dollar_amount_there_is():
    deadline = report(next_action="Ask for the bank statements within 14 days, by Nov 24.")
    new_limit = report(next_action="Set the limit to $58,000 within 14 days.")
    cited = report(
        next_action="Set the limit to $58,000 within 14 days.",
        review_reasons=[{"reason": "large_cut", "finding": "The limit is cut.", "evidence_ids": ["sig_limit"]}],
    )

    assert made_up(deadline, CASE) == []
    assert made_up(new_limit, CASE) == [("next_action", "$58,000", "sig_limit")]
    assert made_up(cited, CASE) == []


def test_the_pilots_sentence_is_refused_for_the_figures_no_record_holds():
    # The 90K incident's summary. On the test signal no record holds $310,000 or $90,000, and $80,000 is not cited.
    draft = report(
        summary=(
            "Weekly inflows of about $80,000 would make true outflows about $310,000, "
            "so roughly $90,000 is not accounted for."
        ),
        evidence=[claim("Records and balances do not match.", "bal_c_010")],
    )

    assert made_up(draft, CASE) == [
        ("summary", "$80,000", "agg_c_010_inflows"),
        ("summary", "$310,000", None),
        ("summary", "$90,000", None),
    ]
