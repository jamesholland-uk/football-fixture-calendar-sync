def parse_spond_host_slots(raw: str) -> list[list[str]]:
    """Comma separates teams; pipe separates multiple hosts on the same team.

    Example: ``idA|idB,idC`` → team 0 hosts [idA, idB], team 1 host [idC].
    A single id per team (``idA,idB``) still works.
    """
    return [
        [host.strip() for host in slot.split("|") if host.strip()]
        for slot in raw.split(",")
    ]
