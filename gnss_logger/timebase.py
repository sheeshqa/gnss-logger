"""No external leap-second table: conversion only when RAWX declares leapS valid."""

from datetime import datetime, timedelta, timezone


GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc)


def rawx_utc(epoch):
    if not epoch["leap_seconds_valid"]:
        return None
    return GPS_EPOCH + timedelta(weeks=epoch["week"], seconds=epoch["tow_s"] - epoch["leap_seconds"])


def parse_utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must have an explicit UTC offset")
    return parsed.astimezone(timezone.utc)
