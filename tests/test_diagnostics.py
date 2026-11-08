import unittest
from unittest.mock import Mock, patch

from card_studio.diagnostics import collect_diagnostics, format_diagnostics
from card_studio.pcsc import MacOSPcscBackend


class DiagnosticsTests(unittest.TestCase):
    @patch("card_studio.diagnostics.shutil.which", return_value=None)
    @patch("card_studio.diagnostics.create_pcsc_backend")
    def test_macos_report_has_no_credentials_or_hardware_claims(self, factory, _which):
        backend = Mock(spec=MacOSPcscBackend)
        backend.list_readers.return_value = []
        factory.return_value = backend
        report = collect_diagnostics(platform="darwin")
        self.assertEqual(report["platform"], "macos")
        self.assertFalse(report["credentialsIncluded"])
        self.assertIn("Application Support", report["paths"]["data"])
        pcsc = next(check for check in report["checks"] if check["name"] == "Lectores PC/SC")
        self.assertEqual(pcsc["status"], "AVISO")
        self.assertIn("Sin lectores", pcsc["detail"])
        self.assertIn("PC/SC", format_diagnostics(report))


if __name__ == "__main__":
    unittest.main()
