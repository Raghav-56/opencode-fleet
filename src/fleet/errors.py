"""Error types shared across the package."""


class FleetError(Exception):
    """A problem worth showing the user as a one-line message.

    Anything raised as a FleetError is expected: a missing agent, a dead
    session, a bad name. The CLI prints these without a traceback. Anything else
    escaping to the top level is a bug in fleet.
    """
