"""POST /reports/monthly/{YYYY-MM}: make and keep a month's credit review report now (app/reports.py).

The service also makes last month's on its own (app/main.py). A month that has not ended is reported to date and
says so. Every run keeps a new copy; none is overwritten.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app import reports
from app.auth import User, current_user

router = APIRouter()

# A report covers every customer's cases, so it is a manager's or an admin's to ask for.
MAY_GENERATE = ("manager", "admin")


class MonthlyReport(BaseModel):
    month: str
    month_to_date: bool
    key: str
    # s3://bucket/key, or the file's path when reports are kept in a folder.
    location: str
    # A download link that works for reports.LINK_SECONDS. None for a report kept in a folder.
    url: str | None
    sha256: str
    generated_at: datetime
    generated_by: str
    totals: dict


@router.post("/reports/monthly/{month}", response_model=MonthlyReport, status_code=status.HTTP_201_CREATED)
def monthly_report(month: str, user: User = Depends(current_user)) -> dict:
    if user.role not in MAY_GENERATE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"The role {user.role} may not make reports")
    try:
        return reports.generate(month, by=user.sub)
    except reports.BadMonth as error:
        # A plain 422, as in signals.py: Starlette renamed its constant.
        raise HTTPException(422, str(error)) from error
    except reports.AlreadyStored as error:
        raise HTTPException(status.HTTP_409_CONFLICT, "A report was made this very second. Try again.") from error
    except reports.NoStorage as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from error
