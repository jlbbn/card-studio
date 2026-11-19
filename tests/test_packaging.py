import ast
import hashlib
import unittest
from pathlib import Path


def _analysis_keyword(tree, keyword):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "Analysis":
            continue
        for item in node.keywords:
            if item.arg == keyword:
                return item.value
    raise AssertionError(f"Analysis no define {keyword}")


def _project_root_path(node):
    parts = []
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        if not isinstance(node.right, ast.Constant) or not isinstance(node.right.value, str):
            return None
        parts.append(node.right.value)
        node = node.left
    if not isinstance(node, ast.Name) or node.id != "project_root":
        return None
    return "/".join(reversed(parts))


def _project_root_data_entries(datas):
    entries = {}
    for item in datas.elts:
        if not isinstance(item, ast.Tuple) or len(item.elts) != 2:
            continue
        source, destination = item.elts
        if not isinstance(source, ast.Call) or not isinstance(source.func, ast.Name):
            continue
        if source.func.id != "str" or len(source.args) != 1:
            continue
        relative_path = _project_root_path(source.args[0])
        if relative_path is not None:
            entries[relative_path] = ast.literal_eval(destination)
    return entries


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

    def test_macos_spec_bundles_gpjar_at_the_runtime_path(self):
        root = Path(__file__).resolve().parents[1]
        spec_path = root / "packaging/macos/card-studio.spec"
        tree = ast.parse(spec_path.read_text(), filename=str(spec_path))
        datas = _analysis_keyword(tree, "datas")
        entries = _project_root_data_entries(datas)
        relative_path = "vendor/GlobalPlatformPro-next/tool/target/gp.jar"

        self.assertEqual(
            entries.get(relative_path),
            "vendor/GlobalPlatformPro-next/tool/target",
        )
        gp_jar = root / relative_path
        self.assertTrue(gp_jar.is_file())
        self.assertEqual(
            hashlib.sha256(gp_jar.read_bytes()).hexdigest(),
            "49bc6c6c009420a0ba70c2305cdd5e80c7ad89152f35a333f6d25d57cfcc8e1c",
        )

    def test_macos_spec_bundles_jpype_native_and_support_dependencies(self):
        root = Path(__file__).resolve().parents[1]
        spec_path = root / "packaging/macos/card-studio.spec"
        spec = spec_path.read_text()
        tree = ast.parse(spec, filename=str(spec_path))
        hiddenimports = set(ast.literal_eval(_analysis_keyword(tree, "hiddenimports")))

        self.assertLessEqual({"_jpype", "jpype", "packaging.version"}, hiddenimports)
        self.assertIn('find_spec("jpype")', spec)

        datas = _analysis_keyword(tree, "datas")
        support_destinations = []
        for item in datas.elts:
            if not isinstance(item, ast.Tuple) or len(item.elts) != 2:
                continue
            source, destination = item.elts
            if not isinstance(source, ast.Call) or len(source.args) != 1:
                continue
            if isinstance(source.args[0], ast.Name) and source.args[0].id == "jpype_support_jar":
                support_destinations.append(ast.literal_eval(destination))
        self.assertEqual(support_destinations, ["."])

        support_assignment = next(
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "jpype_support_jar" for target in node.targets)
        )
        support_constants = {
            node.value
            for node in ast.walk(support_assignment)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertIn("org.jpype.jar", support_constants)

    def test_gui_module_remains_importable(self):
        try:
            import card_studio.app  # noqa: F401
        except ImportError as error:
            self.skipTest(f"GTK no disponible: {error}")


if __name__ == "__main__":
    unittest.main()
