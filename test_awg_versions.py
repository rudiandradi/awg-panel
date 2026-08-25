import unittest

import server

BASE = ["private", "public", "36190", "5", "10", "50", "95", "24", "32", "12", "1", "2", "3", "4"]

class AwgVersionTests(unittest.TestCase):
    def test_awg2_has_no_awg3_fields(self):
        interface = server.parse_interface_dump("\t".join(BASE) + "\n")
        config = server.build_client_config("test", "10.8.1.2", "private", "psk", interface)
        self.assertEqual(interface["protocolVersion"], "2")
        self.assertNotIn("HeaderProtectionKey", config)

    def test_awg3_mirrors_extended_fields(self):
        extension = ["(null)"] * 5 + ["header-key", "10-100", "100-120", "3-7", "150-180", "5-15", "15-20", "on", "on", "off"]
        interface = server.parse_interface_dump("\t".join(BASE + extension) + "\n")
        config = server.build_client_config("test", "10.8.1.2", "private", "psk", interface)
        expected = ("HeaderProtectionKey", "ContentPaddingAddition", "RekeyAfterTime", "RekeyTimeout", "RejectAfterTime", "KeepaliveTimeout", "MaxHandshakeAttempts", "RandomTrailers", "DisableCookies")
        self.assertEqual(interface["protocolVersion"], "3")
        for field in expected:
            self.assertIn(field + " = ", config)


if __name__ == "__main__":
    unittest.main()
