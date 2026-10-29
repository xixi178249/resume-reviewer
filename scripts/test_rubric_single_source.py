"""rubric 单一真源的守卫测试：配置能读、代码只用配置、prompt 与配置一致。

为什么值得单独一个套件：
    v1.7 / v1.8 各出过一次「代码改了 60 字、prompt 还写 40 字」的自伤。这类
    错误不会被功能测试抓到（两边都"能跑"），只会让报告的说辞与判定互相矛盾。
    这里用三种方式把口子堵住：
      1. 代码里的常量必须**等于**配置里的值（不是"读到了就行"）
      2. 三个模块源码里不允许再出现这些常量的字面量（防止改回去）
      3. prompt 模板里写的数字必须是配置里的数字（60 字、6~200 字）

运行：
    python scripts/test_rubric_single_source.py
    python scripts/run_tests.py --suite rubric
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_output  # noqa: E402
import ingest  # noqa: E402
import rubric  # noqa: E402
import version_diff  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PROMPT_DIR = ROOT / "prompts"


class RubricParseTest(unittest.TestCase):
    """受限 YAML 子集解析器本身的行为。"""

    def test_parses_nested_and_scalars(self):
        data = rubric.parse(
            "a: 1\nb:\n  c: 2.5\n  d: hello\n  e: true\nf:\n  g:\n    h: 60\n"
        )
        self.assertEqual(data["a"], 1)
        self.assertEqual(data["b"]["c"], 2.5)
        self.assertEqual(data["b"]["d"], "hello")
        self.assertIs(data["b"]["e"], True)
        self.assertEqual(data["f"]["g"]["h"], 60)

    def test_parses_inline_list(self):
        data = rubric.parse("x: [1, 2, three]\n")
        self.assertEqual(data["x"], [1, 2, "three"])

    def test_strips_comments_only_outside_quotes(self):
        data = rubric.parse('a: 1  # 注释\nb: "带 # 号"  # 注释\n')
        self.assertEqual(data["a"], 1)
        self.assertEqual(data["b"], "带 # 号")

    def test_duplicate_key_raises(self):
        with self.assertRaises(rubric.RubricError) as ctx:
            rubric.parse("a: 1\na: 2\n")
        self.assertIn("重复", str(ctx.exception))

    def test_unsupported_line_raises_with_lineno(self):
        with self.assertRaises(rubric.RubricError) as ctx:
            rubric.parse("a: 1\n这不是 yaml\n")
        msg = str(ctx.exception)
        self.assertIn(":2", msg, "报错要带行号，否则改配置像猜谜")

    def test_missing_key_names_the_path(self):
        with self.assertRaises(rubric.RubricError) as ctx:
            rubric.get("gates.hr.不存在的键")
        self.assertIn("gates.hr.不存在的键", str(ctx.exception))

    def test_num_rejects_non_number(self):
        # format 是一个 section（dict），不是数字；拿它做阈值必然出诡异结果
        with self.assertRaises(rubric.RubricError) as ctx:
            rubric.num("format")
        self.assertIn("不是数字", str(ctx.exception))

    def test_real_file_loads_and_has_sections(self):
        data = rubric.load()
        for section in ("format", "readability", "evidence", "gates", "verdict",
                        "weights", "version_diff", "ingest", "eval"):
            self.assertIn(section, data, f"rubric.yaml 缺少 section：{section}")


class CodeUsesRubricTest(unittest.TestCase):
    """代码里的常量必须等于配置值（不是"大概相等"）。"""

    def test_check_output_constants(self):
        self.assertEqual(check_output.PAGES_CHARS, rubric.num("format.pages_chars"))
        self.assertEqual(
            check_output.LONG_SENTENCE_MIN, rubric.num("readability.long_sentence_chars"))
        self.assertEqual(
            check_output.WEAK_LINE_MIN_REST, rubric.num("readability.weak_line_min_rest"))
        self.assertEqual(check_output.MIN_QUOTE, rubric.num("evidence.min_quote"))
        self.assertEqual(check_output.MAX_QUOTE, rubric.num("evidence.max_quote"))
        self.assertEqual(check_output.HEAD_CHARS, rubric.num("evidence.head_chars"))
        self.assertEqual(check_output.REWRITES_MIN, rubric.num("output.rewrites_min"))
        self.assertEqual(check_output.GATE_ROLE_MIN, rubric.num("gates.hr.role_fit_min"))
        self.assertEqual(check_output.GATE_IMPACT_MIN, rubric.num("gates.hr.impact_min"))
        self.assertEqual(
            check_output.GATE_EQ_MIN, rubric.num("gates.interview.evidence_quality_min"))
        self.assertEqual(
            check_output.GATE_HIGH_RISK_MAX, rubric.num("gates.interview.high_risk_max"))

    def test_version_diff_constants(self):
        self.assertAlmostEqual(
            version_diff.MIN_SIMILARITY, rubric.num("version_diff.min_similarity"), places=9)
        self.assertEqual(version_diff.MAX_CANDIDATES,
                         rubric.num("version_diff.max_candidates"))
        self.assertEqual(version_diff.MAX_DIFF_ITEMS,
                         rubric.num("version_diff.max_diff_items"))
        self.assertEqual(version_diff.MIN_PROBE, rubric.num("version_diff.min_probe"))

    def test_ingest_constant(self):
        self.assertEqual(ingest.MIN_TEXT_CHARS, rubric.num("ingest.min_text_chars"))

    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(check_output.WEIGHTS.values()), 1.0, places=9)
        self.assertEqual(len(check_output.WEIGHTS), 5)

    def test_verdict_bands_match_config(self):
        bands = dict((name, floor) for floor, name in check_output.VERDICT_BANDS)
        self.assertEqual(bands["strong_pass"], rubric.num("verdict.strong_pass_min"))
        self.assertEqual(bands["pass"], rubric.num("verdict.pass_min"))
        self.assertEqual(bands["borderline"], rubric.num("verdict.borderline_min"))
        self.assertEqual(bands["reject"], 0)

    def test_no_hardcoded_numbers_left(self):
        """三个模块里这些常量必须是 rubric 调用，不能是字面量（防回退）。"""
        names = [
            "PAGES_CHARS", "GATE_ROLE_MIN", "GATE_IMPACT_MIN", "GATE_EQ_MIN",
            "GATE_HIGH_RISK_MAX", "HEAD_CHARS", "LONG_SENTENCE_MIN", "MIN_QUOTE",
            "MAX_QUOTE", "WEAK_LINE_MIN_REST", "MIN_SIMILARITY", "MAX_CANDIDATES",
            "MAX_DIFF_ITEMS", "MIN_PROBE", "MIN_TEXT_CHARS", "REWRITES_MIN",
        ]
        pat = re.compile(r"^\s*(?P<name>%s)\s*=\s*[\d.]+" % "|".join(names), re.M)
        offenders = []
        for path in ("check_output.py", "version_diff.py", "ingest.py"):
            text = (Path(__file__).resolve().parent / path).read_text(encoding="utf-8")
            offenders += [f"{path}: {m.group(0).strip()}" for m in pat.finditer(text)]
        self.assertEqual(offenders, [], "这些阈值只能来自 config/rubric.yaml：" + str(offenders))


class PromptMatchesRubricTest(unittest.TestCase):
    """prompt 模板里写的数字必须与配置一致（这是最容易漏的一环）。"""

    def setUp(self):
        self.version = rubric.get("prompt_version")
        self.prompt = (PROMPT_DIR / f"system-{self.version}.md").read_text(encoding="utf-8")

    def test_prompt_file_exists(self):
        self.assertTrue((PROMPT_DIR / f"system-{self.version}.md").exists(),
                        f"rubric.yaml 里 prompt_version={self.version}，但模板文件不存在")

    def test_long_sentence_threshold_appears(self):
        n = int(rubric.num("readability.long_sentence_chars"))
        self.assertIn(f"{n} 字内", self.prompt,
                      f"prompt 里必须出现「{n} 字内」（检验 2 的长句阈值）")

    def test_quote_bounds_appear(self):
        lo = int(rubric.num("evidence.min_quote"))
        hi = int(rubric.num("evidence.max_quote"))
        self.assertIn(f"{lo}~{hi} 字", self.prompt,
                      f"prompt 里必须出现「{lo}~{hi} 字」（引用长度区间）")

    def test_no_stale_threshold_in_prompt(self):
        """历史阈值 40 字不该再作为长句判据出现（只在说明里提到可以）。"""
        n = int(rubric.num("readability.long_sentence_chars"))
        bad = [m.group(0) for m in re.finditer(r"每句\s*(\d+)\s*字内", self.prompt)
               if int(m.group(1)) != n]
        self.assertEqual(bad, [], f"prompt 里出现了过期阈值：{bad}（应为 {n}）")


class SchemaMatchesRubricTest(unittest.TestCase):
    """JSON Schema 里的长度与条数限制必须与配置一致。

    这是「三处一致」的第三处。v2.0 之前只查了代码与 prompt，于是 Schema 里
    evidence 写着 minLength=4、rubric 写着 6，模型填 5 个字先过 Schema、
    再被代码判成不合格——两边都"没错"，用户只会看到一份莫名其妙被拒的报告。
    """

    def setUp(self):
        self.version = rubric.get("prompt_version")
        path = PROMPT_DIR / f"output-schema-{self.version}.json"
        self.assertTrue(path.exists(),
                        f"rubric.yaml 里 prompt_version={self.version}，但 Schema 文件不存在")
        self.schema = json.loads(path.read_text(encoding="utf-8"))

    def _at(self, *path):
        node = self.schema
        for key in path:
            self.assertIn(key, node, f"Schema 缺少路径：{'/'.join(path)}")
            node = node[key]
        return node

    def test_evidence_min_length_matches(self):
        lo = int(rubric.num("evidence.min_quote"))
        for holder in ("hard_blockers", "issues"):
            node = self._at("properties", holder, "items", "properties", "evidence")
            self.assertEqual(node.get("minLength"), lo,
                             f"{holder}[].evidence.minLength 必须等于 evidence.min_quote（{lo}）")

    def test_evidence_max_length_matches(self):
        hi = int(rubric.num("evidence.max_quote"))
        lo = int(rubric.num("evidence.min_quote"))
        # number_audit 里引用原文的字段叫 context（不是 evidence）
        node = self._at("properties", "number_audit", "items", "properties", "context")
        self.assertEqual(node.get("maxLength"), hi,
                         f"number_audit[].context.maxLength 必须等于 evidence.max_quote（{hi}）")
        self.assertEqual(node.get("minLength"), lo,
                         f"number_audit[].context.minLength 必须等于 evidence.min_quote（{lo}）")

    def test_rewrites_count_matches(self):
        node = self._at("properties", "rewrites")
        self.assertEqual(node.get("minItems"), int(rubric.num("output.rewrites_min")))
        self.assertEqual(node.get("maxItems"), int(rubric.num("output.rewrites_max")))


class GuardTest(unittest.TestCase):
    """配置本身写错时要在加载阶段就炸，而不是让评测结果变得诡异。"""

    def _with_config(self, text: str):
        """临时把 CONFIG_PATH 指向一份配置，用完恢复。"""
        tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
        tmp.write(text)
        tmp.close()
        original = rubric.CONFIG_PATH
        rubric.CONFIG_PATH = Path(tmp.name)
        rubric.reload()

        def restore():
            rubric.CONFIG_PATH = original
            rubric.reload()
            Path(tmp.name).unlink(missing_ok=True)

        self.addCleanup(restore)

    def test_weights_must_sum_to_100(self):
        self._with_config(
            "weights:\n  a: 25\n  b: 25\n  c: 20\n  d: 15\n  e: 10\n"
        )
        with self.assertRaises(rubric.RubricError) as ctx:
            rubric.weights()
        self.assertIn("100", str(ctx.exception))

    def test_verdict_bands_must_descend(self):
        self._with_config(
            "verdict:\n  strong_pass_min: 50\n  pass_min: 65\n  borderline_min: 80\n"
        )
        with self.assertRaises(rubric.RubricError) as ctx:
            rubric.verdict_bands()
        self.assertIn("verdict", str(ctx.exception))

    def test_reload_reads_new_value(self):
        self._with_config("readability:\n  long_sentence_chars: 72\n")
        self.assertEqual(rubric.num("readability.long_sentence_chars"), 72)


if __name__ == "__main__":
    unittest.main(verbosity=2)
