"""
In-memory demo data for the example tools.

Stands in for the HR system the real tools talked to, so the server runs
end to end with nothing but Redis. All people and numbers are made up.
"""
from typing import Dict, List

EMPLOYEES: List[Dict] = [
    {"employee_id": "E1001", "first_name": "Thandi", "last_name": "Nkosi", "email": "thandi.nkosi@example.org", "active": True},
    {"employee_id": "E1002", "first_name": "Pieter", "last_name": "van Wyk", "email": "pieter.vanwyk@example.org", "active": True},
    {"employee_id": "E1003", "first_name": "Lerato", "last_name": "Mokoena", "email": "lerato.mokoena@example.org", "active": True},
    {"employee_id": "E1004", "first_name": "Lerato", "last_name": "Mokoena", "email": "lerato.m@example.org", "active": False},
]

# employee_id -> leave type -> days remaining
LEAVE_BALANCES: Dict[str, Dict[str, float]] = {
    "E1001": {"annual": 14.0, "sick": 28.0, "family": 3.0},
    "E1002": {"annual": 6.5, "sick": 30.0, "family": 3.0},
    "E1003": {"annual": 21.0, "sick": 25.0, "family": 1.0},
}
