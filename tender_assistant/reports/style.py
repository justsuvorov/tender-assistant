"""Shared color palette for report writers (DMS and cargo reconciliation)."""

GREEN = "D6F0D6"
RED = "F0D6D6"
YELLOW = "FFF3CD"
HEADER = "1F4E79"  # dark blue for header background


def status_fill(status: str, fill_map: dict[str, str]) -> str:
    return fill_map.get(status.lower().strip(), "FFFFFF")
