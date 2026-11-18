import unittest
from pathlib import Path


class PackagingTests(unittest.TestCase):
    def test_macos_packaging_covers_app_dmg_and_both_architectures(self):
        root = Path(__file__).resolve().parents[1]
        spec = (root / "packaging/macos/card-studio.spec").read_text()
        build = (root / "packaging/macos/build.sh").read_text()
        self.assertIn("BUNDLE", spec)
        self.assertIn('name=f"{app_name}.app"', spec)
        self.assertIn('APP="$DIST/Swift Ledger Card Studio.app"', build)
        self.assertIn("hdiutil create", build)
        self.assertIn("arm64", build)
        self.assertIn("x86_64", build)
        self.assertIn("unittest discover", build)

    def test_macos_spec_resolves_the_project_root(self):
        root = Path(__file__).resolve().parents[1]
        spec_path = root / "packaging/macos/card-studio.spec"
        spec = spec_path.read_text()
        self.assertIn("Path(SPECPATH).resolve().parents[1]", spec)
        self.assertNotIn("Path(SPECPATH).resolve().parents[2]", spec)
        specpath = spec_path.resolve().parent
        self.assertEqual(specpath.parents[1], root)
        self.assertTrue((specpath.parents[1] / "card_studio_app.py").is_file())

    def test_gui_module_remains_importable(self):
        try:
            import card_studio.app  # noqa: F401
        except ImportError as error:
            self.skipTest(f"GTK no disponible: {error}")


if __name__ == "__main__":
    unittest.main()
