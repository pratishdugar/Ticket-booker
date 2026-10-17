import pytest

from ticketbooker.models import AvailabilityStatus as A
from ticketbooker.models import parse_availability


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("AVAILABLE-0045", (A.AVAILABLE, 45)),
        ("AVL 12", (A.AVAILABLE, 12)),
        ("CURR_AVBL-0003", (A.AVAILABLE, 3)),
        ("RAC 12", (A.RAC, 12)),
        ("RAC12/RAC5", (A.RAC, 12)),
        ("GNWL23/WL10", (A.WAITLIST, 10)),
        ("WL 7", (A.WAITLIST, 7)),
        ("PQWL5", (A.WAITLIST, 5)),
        ("REGRET/WL", (A.REGRET, None)),
        ("Train Departed", (A.REGRET, None)),
        (14, (A.AVAILABLE, 14)),
        ("0", (A.REGRET, 0)),
        (None, (A.UNKNOWN, None)),
        ("something odd", (A.UNKNOWN, None)),
    ],
)
def test_parse_availability(raw, expected):
    assert parse_availability(raw) == expected
