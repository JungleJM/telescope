"""Duration formatting and the status vocabulary."""

from __future__ import annotations

import unittest

from ..models import (
    ALL_STATUSES,
    SETTLED_STATUSES,
    DONE,
    SKIPPED,
    StatusError,
    duration_block,
    format_duration,
    validate_status,
)


class FormatDurationTests(unittest.TestCase):
    def test_renders_expected_shapes(self):
        cases = [
            (0, "0s"),
            (45, "45s"),
            (60, "1m 0s"),
            (132, "2m 12s"),
            (332, "5m 32s"),
            (3600, "1h 0m 0s"),
            (3723, "1h 2m 3s"),
        ]
        for seconds, expected in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(format_duration(seconds), expected)

    def test_rounds_fractional_seconds(self):
        self.assertEqual(format_duration(45.4), "45s")
        self.assertEqual(format_duration(45.6), "46s")


class DurationBlockTests(unittest.TestCase):
    def test_builds_block_from_timestamps(self):
        self.assertEqual(
            duration_block("2026-09-22T14:03:00-05:00", "2026-09-22T14:08:32-05:00"),
            {"seconds": 332, "display": "5m 32s"},
        )

    def test_handles_offset_differences(self):
        # Same instant expressed in two zones is a zero-length duration.
        self.assertEqual(
            duration_block("2026-09-22T14:00:00-05:00", "2026-09-22T15:00:00-04:00"),
            {"seconds": 0, "display": "0s"},
        )

    def test_returns_none_without_both_timestamps(self):
        self.assertIsNone(duration_block(None, "2026-09-22T14:08:32-05:00"))
        self.assertIsNone(duration_block("2026-09-22T14:03:00-05:00", None))
        self.assertIsNone(duration_block(None, None))


class StatusTests(unittest.TestCase):
    def test_accepts_every_known_status(self):
        for status in ALL_STATUSES:
            with self.subTest(status=status):
                self.assertEqual(validate_status(status), status)

    def test_rejects_unknown_status(self):
        with self.assertRaises(StatusError):
            validate_status("finished")

    def test_settled_statuses_are_the_ones_resume_skips(self):
        self.assertEqual(set(SETTLED_STATUSES), {DONE, SKIPPED})
