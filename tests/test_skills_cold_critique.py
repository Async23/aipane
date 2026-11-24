#!/usr/bin/env python3
"""Bridge skills/aipane-cold-critique tests and basic skill metadata checks into top-level unittest discovery."""
from __future__ import annotations

from pathlib import Path
import re
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "skills"
COLD_CRITIQUE_TESTS_DIR = SKILLS_DIR / "aipane-cold-critique" / "tests"
FRONTMATTER_NAME_RE = re.compile(r"^name:\s*['\"]?([a-z0-9-]+)['\"]?\s*$", re.MULTILINE)


class SkillsContractTests(unittest.TestCase):
    def test_all_skills_have_matching_frontmatter_and_readme_entry(self) -> None:
        skill_dirs = sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir() and not p.name.startswith("."))
        self.assertGreaterEqual(len(skill_dirs), 8)
        index_readme = (SKILLS_DIR / "README.md").read_text(encoding="utf-8")

        for skill_dir in skill_dirs:
            self.assertTrue(skill_dir.name.startswith("aipane-"), f"unexpected skill prefix: {skill_dir.name}")
            skill_md = skill_dir / "SKILL.md"
            self.assertTrue(skill_md.is_file(), f"missing SKILL.md in {skill_dir}")
            text = skill_md.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\n"), f"missing YAML frontmatter in {skill_md}")
            header = text.split("\n---", 1)[0]
            match = FRONTMATTER_NAME_RE.search(header)
            self.assertIsNotNone(match, f"missing frontmatter name in {skill_md}")
            self.assertEqual(match.group(1), skill_dir.name)
            self.assertIn(f"`{skill_dir.name}`", index_readme)
            self.assertIn(f"({skill_dir.name}/SKILL.md)", index_readme)


def load_tests(loader: unittest.TestLoader, standard_tests: unittest.TestSuite, pattern: str | None) -> unittest.TestSuite:
    suite = unittest.TestSuite()
    suite.addTests(standard_tests)
    skill_suite = loader.discover(
        start_dir=str(COLD_CRITIQUE_TESTS_DIR),
        pattern="test_*.py",
        top_level_dir=str(COLD_CRITIQUE_TESTS_DIR),
    )
    if skill_suite.countTestCases() == 0:
        raise AssertionError(f"no tests discovered under {COLD_CRITIQUE_TESTS_DIR}")
    suite.addTests(skill_suite)
    return suite


if __name__ == "__main__":
    unittest.main()
