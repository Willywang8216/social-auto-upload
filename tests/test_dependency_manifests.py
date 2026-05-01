from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent.parent
PYPROJECT_PATH = ROOT / "pyproject.toml"
REQUIREMENTS_PATH = ROOT / "requirements.txt"
EXPECTED_QR_RUNTIME_DEPS = {
    "opencv-python-headless==4.13.0.92",
    "segno==1.6.6",
}


class DependencyManifestDriftTests(unittest.TestCase):
    def test_pyproject_and_requirements_include_matching_qr_runtime_deps(self) -> None:
        pyproject_dependencies = self._read_pyproject_dependencies()
        requirements_dependencies = self._read_requirements_dependencies()

        for dependency in EXPECTED_QR_RUNTIME_DEPS:
            with self.subTest(dependency=dependency):
                self.assertIn(dependency, pyproject_dependencies)
                self.assertIn(dependency, requirements_dependencies)

    def _read_pyproject_dependencies(self) -> set[str]:
        dependencies: set[str] = set()
        in_dependencies_block = False

        for raw_line in PYPROJECT_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()

            if not in_dependencies_block:
                if line == "dependencies = [":
                    in_dependencies_block = True
                continue

            if line == "]":
                break

            if line.startswith('"') and line.endswith('",'):
                dependencies.add(line.removeprefix('"').removesuffix('",'))

        return dependencies

    def _read_requirements_dependencies(self) -> set[str]:
        dependencies: set[str] = set()
        for raw_line in REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            dependencies.add(line)
        return dependencies


if __name__ == "__main__":
    unittest.main()
