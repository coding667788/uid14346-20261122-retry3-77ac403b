"""End-to-end CLI tests driving ``python -m solution`` via main()."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest

from solution.__main__ import main

from tests.helpers import base_case


class CliTests(unittest.TestCase):
    def _write(self, doc: dict) -> str:
        fd, path = tempfile.mkstemp(suffix=".json", prefix="case_")
        with io.open(fd, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, ensure_ascii=False)
        self.addCleanup(os.unlink, path)
        return path

    def _capture(self, argv: list[str]) -> tuple[int, dict]:
        buffer = io.BytesIO()
        wrapper = io.TextIOWrapper(buffer, encoding="utf-8", write_through=True)
        old_stdout = sys.stdout
        sys.stdout = wrapper
        try:
            code = main(argv)
        finally:
            sys.stdout = old_stdout
            wrapper.detach()
        payload = json.loads(buffer.getvalue().decode("utf-8"))
        return code, payload

    def test_bad_usage(self):
        code, payload = self._capture(["frobnicate", "x.json"])
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], "BAD_USAGE")

    def test_missing_file(self):
        code, payload = self._capture(
            ["plan", os.path.join(tempfile.gettempdir(), "no_such_case_8421.json")]
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["error_code"], "FILE_NOT_FOUND")

    def test_invalid_json_reports_position(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        with io.open(fd, "w", encoding="utf-8") as handle:
            handle.write("{ broken")
        self.addCleanup(os.unlink, path)
        code, payload = self._capture(["plan", path])
        self.assertEqual(code, 1)
        self.assertEqual(payload["error_code"], "INVALID_JSON")
        self.assertIn("line", payload["field"])

    def test_structural_error_payload(self):
        doc = base_case()
        del doc["domains"]
        path = self._write(doc)
        code, payload = self._capture(["plan", path])
        self.assertEqual(code, 1)
        self.assertEqual(payload["error_code"], "MISSING_FIELD")
        self.assertEqual(payload["field"], "domains")

    def test_plan_output_is_json_and_reproducible(self):
        path = self._write(base_case())
        code1, payload1 = self._capture(["plan", path])
        code2, payload2 = self._capture(["plan", path])
        self.assertEqual((code1, code2), (0, 0))
        self.assertEqual(
            json.dumps(payload1, sort_keys=True),
            json.dumps(payload2, sort_keys=True),
        )

    def test_utf8_output_with_non_ascii_case_id_round_trips(self):
        doc = base_case(case_id="虚构-备份-案例-①")
        path = self._write(doc)
        code, payload = self._capture(["evaluate", path])
        self.assertEqual(code, 0)
        self.assertEqual(payload["case_id"], "虚构-备份-案例-①")

    def test_check_command_on_embedded_schedule(self):
        doc = base_case()
        doc["schedule"] = [{"type": "wait"}]
        path = self._write(doc)
        code, payload = self._capture(["check", path])
        # A wait-only schedule cannot retire T_OLD, so it is invalid, but the
        # checker must still return structured JSON rather than raising.
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "invalid")
        self.assertEqual(
            payload["first_violation"]["violation_code"],
            "RETIREMENT_NOT_ACHIEVED",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
