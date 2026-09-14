"""Conservative day attribution for source suspension reports containing plans."""
from datetime import date


def classify_suspension(details, query_date: date, completed_through: date) -> str:
    """Do not infer active suspension from membership or an open-ended old start.

    Only a completed query day with an explicit matching start or a closed
    source-reported date span supports 'suspended' (not necessarily all day).
    Future starts support 'announced'; other records remain 'unknown'.
    Predicted resumption is never used as a confirmed end.
    """
    planned = False
    for detail in details:
        start = date.fromisoformat(detail['停牌时间']) if detail.get('停牌时间') else None
        end = date.fromisoformat(detail['停牌截止时间']) if detail.get('停牌截止时间') else None
        if start is None or (end is not None and end < start):
            continue
        if start > query_date or start > completed_through:
            planned = True
            continue
        if query_date <= completed_through:
            if start == query_date or (end is not None and start <= query_date <= end):
                return 'suspended'
    return 'announced' if planned else 'unknown'
