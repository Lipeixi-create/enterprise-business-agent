from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from enterprise_business_agent import models  # noqa: F401
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base
from enterprise_business_agent.quality import run_quality_checks


def test_generated_dataset_passes_quality_checks():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        counts = generate_data(session, date(2024, 1, 1), 24, reset=False)
        report = run_quality_checks(session, date(2024, 1, 1), 24)
    assert report.passed, report.errors
    assert counts["sales_fact"] == 24 * 4 * 4 * 8
    assert counts["finance_monthly"] == 24 * 4
    assert counts["inventory_snapshot"] == 24 * 4 * 8
    assert counts["purchase_fact"] == 24 * 4 * 8
    assert counts["hr_monthly"] == 24 * 5
    assert counts["marketing_fact"] > 0
    assert report.row_counts["marketing_fact"] == counts["marketing_fact"]
    assert report.sample_metric_values["sales_amount"] > 0
    assert report.sample_metric_values["gross_profit"] > 0
    assert report.sample_metric_values["ROI"] > 0
    assert len(report.event_assertions) == 13
    assert all(item.passed for item in report.event_assertions), [
        item.name for item in report.event_assertions if not item.passed
    ]
    assert {
        "overstock_risk_products",
        "marketing_roi_decline_q4",
        "budget_overrun_q4",
        "net_profit_decline_q4",
        "marketing_labor_cost_growth_q4",
    } <= {item.name for item in report.event_assertions}
