"""
多资料合并的回归测试（框架第 7 层：评测）。

为什么这个文件值得单独存在：
合并是唯一一个「错了用户也发现不了」的环节——把上下位关系当成同义合并，
学生的计划里就少一个该学的知识点，而他不会知道自己少了什么。
所以每一条拦截规则都必须有对应用例钉住，改代码时不能悄悄放宽。

跑法：python -m unittest tests.test_merge -v
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import merge  # noqa: E402


def chunk(cid, text, file, src="第 1 页"):
    return {"id": cid, "text": text, "file": file, "source": src, "heading": text}


def point(name, chunks, deps=None, imp=3, diff=3, freq=3, minutes=40):
    return {"name": name, "summary": name, "chunks": chunks, "deps": deps or [],
            "importance": imp, "difficulty": diff, "exam_freq": freq, "minutes": minutes}


CHUNKS = [
    chunk(1, "教材：快速傅里叶变换（FFT）与蝶形运算", "教材.pdf", "第 40 页"),
    chunk(2, "教材：二叉树的遍历", "教材.pdf", "第 50 页"),
    chunk(3, "课件：FFT 分解过程", "第6讲.pptx", "第 12 页"),
    chunk(4, "习题：二叉树遍历的三种方式", "习题册.pdf", "第 3 页"),
    chunk(5, "课件：TCP 三次握手", "第3讲.pptx", "第 8 页"),
    chunk(6, "课件：二叉搜索树的中序遍历", "第3讲.pptx", "第 20 页"),
]


class AliasTest(unittest.TestCase):
    """别名可信度：这是拦模型的最后一道闸，阈值松一格就会误合并。"""

    CASES = [
        # 应该合并
        ("快速傅里叶变换", "FFT", True, "中英缩写"),
        ("TCP", "传输控制协议", True, "中英全称"),
        ("二叉树的遍历", "二叉树遍历", True, "多余助词"),
        ("傅立叶变换", "傅里叶变换", True, "异体字"),
        ("三次握手", "TCP三次握手", True, "简称，多出部分是外文"),
        # 不应合并
        ("二叉搜索树的中序遍历", "二叉树遍历", False, "上下位关系，实测模型会判错"),
        ("傅里叶变换", "快速傅里叶变换", False, "多出的『快速』是修饰语，DFT≠FFT"),
        ("FFT复杂度分析", "快速傅里叶变换", False, "一个讲复杂度，一个是算法本身"),
        ("进程", "线程", False, "相关但不同"),
        ("堆排序", "排序算法", False, "上下位"),
        ("快速排序", "归并排序", False, "并列的两种算法"),
    ]

    def test_cases(self):
        for a, b, want, why in self.CASES:
            with self.subTest(pair=f"{a} / {b}"):
                self.assertEqual(merge._plausible_alias(a, b), want, why)

    def test_symmetric(self):
        """顺序不能影响判定。"""
        for a, b, want, _ in self.CASES:
            self.assertEqual(merge._plausible_alias(b, a), want, f"{b} / {a}")


class RuleMergeTest(unittest.IsolatedAsyncioTestCase):
    async def test_rule_merge_and_coverage(self):
        points = [
            point("快速傅里叶变换（FFT）", [1], imp=4),
            point("FFT", [3], deps=["快速傅里叶变换（FFT）"], freq=4),
            point("二叉树的遍历", [2], imp=4),
            point("二叉树遍历", [4], deps=["二叉树的遍历"]),
            point("堆排序", [2], imp=2),
        ]
        merged, rep = await merge.merge_points(points, CHUNKS, llm=None)
        names = [p["name"] for p in merged]

        self.assertEqual(len(merged), 3, names)
        self.assertNotIn("FFT", names)
        self.assertNotIn("二叉树遍历", names)
        self.assertIn("堆排序", names, "不相关的点不该被牵连")

        fft = next(p for p in merged if "FFT" in p["name"])
        # chunks 求并：溯源不能因为合并而丢
        self.assertEqual(fft["chunks"], [1, 3])
        # 覆盖度按「几份资料」算，不是按 chunk 数
        self.assertEqual(fft["coverage"], 2)
        self.assertEqual(set(fft["files"]), {"第6讲.pptx", "教材.pdf"})
        # 依赖里指向自己的，合并后必须清掉，否则拓扑排序死锁
        self.assertEqual(fft["deps"], [])
        # 分数取 max
        self.assertEqual(fft["exam_freq"], 4)
        # minutes 取 max 而不是求和：学一次就够
        self.assertEqual(fft["minutes"], 40)

        self.assertEqual(rep["merged_count"], 2)
        self.assertEqual(rep["multi_source_count"], 2)
        self.assertTrue(rep["enabled"])

    async def test_single_file_has_no_coverage_bonus(self):
        """单份资料时所有 coverage 都是 1，合并层不该改变行为。"""
        chunks = [chunk(1, "a", "唯一资料.pdf"), chunk(2, "b", "唯一资料.pdf")]
        points = [point("知识点甲", [1]), point("知识点乙", [2])]
        merged, rep = await merge.merge_points(points, chunks, llm=None)
        self.assertEqual(len(merged), 2)
        self.assertTrue(all(p["coverage"] == 1 for p in merged))
        self.assertEqual(rep["multi_source_count"], 0)
        self.assertFalse(rep["enabled"])


class FakeLLM:
    """模拟一个「热情过头」的模型：该合的合，不该合的也合。"""

    available = True

    def __init__(self, groups):
        self.groups = groups

    async def json_call(self, system, user, retries=2):
        return {"groups": self.groups}


class LlmMergeTest(unittest.IsolatedAsyncioTestCase):
    async def test_merges_alias_but_blocks_hyponym(self):
        points = [
            point("TCP三次握手", [5], imp=4),
            point("三次握手", [5]),
            point("二叉搜索树的中序遍历", [6], imp=4),
            point("二叉树遍历", [4], imp=4),
            point("堆排序", [2], imp=2),
        ]
        fake = FakeLLM([
            {"keep": "TCP三次握手", "drop": ["三次握手"]},
            {"keep": "二叉树遍历", "drop": ["二叉搜索树的中序遍历"]},   # 应被拦
            {"keep": "不存在的名字", "drop": ["堆排序"]},               # 模型幻觉，应被丢
        ])
        merged, rep = await merge.merge_points(points, CHUNKS, fake)
        names = [p["name"] for p in merged]

        self.assertIn("TCP三次握手", names)
        self.assertNotIn("三次握手", names, "合法简称应合并")

        self.assertIn("二叉搜索树的中序遍历", names, "上下位关系不能被合并掉")
        self.assertIn("二叉树遍历", names)

        self.assertIn("堆排序", names, "keep 名不存在时整组作废，不能牵连别的点")

        self.assertEqual(rep["mode"], "llm")
        rejected = [r for r in rep["rejected"] if "二叉搜索树的中序遍历" in r["drop"]]
        self.assertTrue(rejected, "被拦下的合并要留痕，便于排查")

    async def test_llm_failure_falls_back_to_rule(self):
        """模型挂了：规则能合的照合，规则合不了的宁可不合（不猜）。"""
        class Broken:
            available = True

            async def json_call(self, *a, **kw):
                raise RuntimeError("timeout")

        points = [
            point("二叉树的遍历", [2]), point("二叉树遍历", [4]),   # 规则层能合
            point("TCP三次握手", [5]), point("三次握手", [5]),      # 只能靠模型
        ]
        merged, rep = await merge.merge_points(points, CHUNKS, Broken())
        names = [p["name"] for p in merged]

        self.assertIn("二叉树的遍历", names, "规则层不该因为模型挂了而失效")
        self.assertNotIn("二叉树遍历", names)
        self.assertIn("三次握手", names, "模型不可用时保守处理，不猜测合并")
        self.assertEqual(rep["mode"], "rule")
        self.assertIn("模型归并失败", rep["note"])


class EndToEndTest(unittest.IsolatedAsyncioTestCase):
    async def test_two_files_through_knowledge_builder(self):
        """解析 → 抽取 → 合并 → 排期，全链路不报错且带覆盖率。"""
        from datetime import date

        from core import extract, knowledge, schedule

        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_files")
        os.makedirs(d, exist_ok=True)
        a, b = os.path.join(d, "教材.txt"), os.path.join(d, "课件.txt")
        with open(a, "w", encoding="utf-8") as f:
            f.write("# 第4章 快速傅里叶变换（FFT）\n\n"
                    "FFT 是离散傅里叶变换的快速算法，蝶形运算是它的核心结构，考试必考推导。\n")
        with open(b, "w", encoding="utf-8") as f:
            f.write("# 第6讲 FFT\n\nFFT 分解过程与复杂度分析，必须掌握蝶形图怎么画。\n")

        data = extract.extract_many([a, b])
        self.assertEqual(len(data["meta"]["files"]), 2)

        class NoLLM:
            available = False

        kn = await knowledge.build_knowledge(data["chunks"], "数据结构", NoLLM())
        self.assertIn("merge", kn)
        self.assertTrue(kn["merge"]["enabled"])
        self.assertTrue(all("coverage" in p for p in kn["points"]))

        plan = schedule.make_plan(kn["points"], date(2026, 12, 20), 120)
        self.assertGreater(len(plan["days"]), 0)
        for day in plan["days"]:
            for it in day["items"]:
                self.assertIn("coverage", it, "排期结果要带上覆盖率，前端才能标『几份资料』")

        for fn in (a, b):
            os.remove(fn)
        os.rmdir(d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
