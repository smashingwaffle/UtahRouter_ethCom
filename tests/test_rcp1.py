import unittest

from utah_eth2rs422 import rcp1

# Captured from a real SC-40 (Serial 2, 19200 8N1): reply to ESC L 000 CR
REAL_REPLY = bytes.fromhex("02 41 40 30 30 30 30 30 30 23 0d 02 4e 4f 33 31 3f 30 30 30 2e 0d 1f")


class TestRcp1(unittest.TestCase):
    def test_checksum_matches_real_router(self):
        self.assertEqual(rcp1.checksum(bytes.fromhex("02 41 40 30 30 30 30 30 30")), 0x23)
        self.assertEqual(rcp1.checksum(bytes.fromhex("02 4e 4f 33 31 3f 30 30 30")), 0x2E)

    def test_levels(self):
        self.assertEqual(rcp1.levels(ord("A"), ord("@")), [1])
        self.assertEqual(rcp1.levels(ord("N"), ord("O")), [2, 3, 4, 5, 6, 7, 8])

    def test_parse_real_reply(self):
        msgs = rcp1.parse_report(REAL_REPLY)
        self.assertEqual(len(msgs), 2)
        self.assertTrue(all(m["ok"] for m in msgs))
        self.assertEqual((msgs[0]["src"], msgs[0]["dst"]), (0, 0))
        self.assertIsNone(msgs[1]["src"])  # "31?" = unassigned levels
        self.assertTrue(rcp1.report_complete(REAL_REPLY))
        self.assertFalse(rcp1.report_has_bad_checksum(REAL_REPLY))

    def test_describe(self):
        self.assertEqual(rcp1.describe_report(REAL_REPLY), "dst 001 <- src 001 (level 1)")
        self.assertEqual(rcp1.describe_request(b"\x1bL013\r"), "Status request dst 014")
        self.assertEqual(rcp1.describe_request(b"\x01A@003005\r"),
                         "TAKE src 004 -> dst 006 (level 1)")
        self.assertEqual(rcp1.describe_request(b"\x01A@003005\r", one_based=False),
                         "TAKE src 003 -> dst 005 (level 1)")

    def test_classify(self):
        self.assertEqual(rcp1.classify_request(b"\x1bL000\r"), "poll")
        self.assertEqual(rcp1.classify_request(b"\x01A@000001\r"), "take")


if __name__ == "__main__":
    unittest.main()
