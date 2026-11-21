"""
视频检索的回归测试。

这个模块最大的风险不是接口挂了（挂了会降级，用户只是看不到视频），
而是**推荐了一条看起来相关、实际讲别的东西的视频**——用户点进去浪费 15 分钟，
而且会认为这个工具的判断不可信。

所以测试重点全在「相关性判定」上，尤其是踩过的那两个坑：
快速排序混进快速傅里叶变换、句法复杂度分析混进 FFT 复杂度分析。

跑法：python -m unittest tests.test_video -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import video  # noqa: E402


class DurationTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(video._secs("22:39"), 22 * 60 + 39)
        self.assertEqual(video._secs("253:27"), 253 * 60 + 27)
        self.assertEqual(video._secs("3:04"), 184)
        self.assertEqual(video._secs(""), 0)
        self.assertEqual(video._secs("abc"), 0)


class RelevanceTest(unittest.TestCase):
    """相关性是硬门槛：过不了直接丢，不参与排序。"""

    def test_marketing_short_video_vs_real_lesson(self):
        """坑一：搜「快速傅里叶变换」，B 站会返回「快速排序」。"""
        spec = video._core_terms("快速傅里叶变换(FFT)")
        self.assertFalse(
            video._relevant("数据结构合集 - 快速排序(算法过程, 效率分析, 稳定性分析)", spec),
            "快速排序不是快速傅里叶变换，哪怕都有『快速』二字",
        )
        self.assertTrue(
            video._relevant("离散傅里叶变换（DFT）与快速傅里叶变换（FFT）本质探索", spec))
        self.assertTrue(
            video._relevant("【数字信号处理考研】基2-DIT-FFT基于时间抽取的快速傅里叶变换算法", spec))

    def test_cross_domain_contamination(self):
        """坑二：搜「FFT复杂度分析」，会返回「句法复杂度分析器」。

        后 4 字截断出来的「杂度分析」正好命中了 —— 所以带缩写的名字
        必须要求缩写在标题里出现。
        """
        spec = video._core_terms("FFT复杂度分析")
        self.assertEqual(spec["required"], ["fft"], "缩写要单独拎出来当必要条件")
        self.assertFalse(
            video._relevant("【过期】NeoSCA：句法复杂度分析器L2SCA分支版本", spec))
        self.assertFalse(
            video._relevant("陆小飞句法复杂度分析器（L2SCA) 单语篇分析教程", spec))

    def test_no_abbreviation_degrades_gracefully(self):
        """没有英文缩写的名字，退回「任一核心词命中」。"""
        spec = video._core_terms("二叉树的遍历")
        self.assertEqual(spec["required"], [])
        self.assertTrue(video._relevant("数据结构合集 - 二叉树的遍历(前序, 中序, 后序)", spec))
        self.assertFalse(video._relevant("图的深度优先遍历/深度优先搜索", spec))

    def test_prefix_suffix_terms(self):
        """长名字靠前 4 / 后 4 字兜住标题里的截断写法。"""
        spec = video._core_terms("中序遍历二叉搜索树")
        self.assertIn("中序遍历", spec["any"])
        self.assertTrue(video._relevant("二叉树的遍历(前序遍历, 中序遍历, 后序遍历)", spec))


class ScoreTest(unittest.TestCase):
    """排序只决定「哪条更值得看」，相关性已由 _relevant 保证。"""

    def _v(self, title, play, duration, danmaku=100):
        return {"title": title, "play": play, "duration": duration,
                "_secs": video._secs(duration), "_danmaku": danmaku}

    def test_lesson_beats_clickbait(self):
        """坑三：71 万播放的「三分钟秒解」不该压过 24 分钟的正式讲解。"""
        spec = video._core_terms("二叉树遍历")
        clickbait = self._v("【有挂！！】二叉树遍历秒解，前序遍历、中序遍历、后序遍历",
                            213827, "3:04")
        lesson = self._v("二叉树遍历的三种题型", 47337, "24:55")
        self.assertGreater(video._score(lesson, spec), video._score(clickbait, spec))

    def test_binge_collection_is_penalized(self):
        """4 小时合集不该排第一——一次看不完，对学生没用。"""
        spec = video._core_terms("傅里叶变换")
        huge = self._v("《信号与系统》4小时快速突击|期末不挂科必备", 2497335, "253:27")
        normal = self._v("傅里叶变换 40 分钟讲透", 20000, "40:00")
        self.assertGreater(video._score(normal, spec), video._score(huge, spec))


class PickTest(unittest.TestCase):
    def test_filters_before_ranking(self):
        """相关性过滤必须发生在排序之前，否则高分垃圾照样进来。"""
        spec = video._core_terms("快速傅里叶变换(FFT)")
        cands = [
            {"title": "快速排序详解", "play": 900000, "duration": "20:00",
             "bvid": "BV1", "_secs": 1200, "_danmaku": 500},
            {"title": "快速傅里叶变换（FFT）推导", "play": 1000, "duration": "18:00",
             "bvid": "BV2", "_secs": 1080, "_danmaku": 10},
        ]
        out = video._pick(cands, spec, 2)
        self.assertEqual([v["bvid"] for v in out], ["BV2"], "高播放的不相关视频必须被丢掉")

    def test_internal_fields_stripped(self):
        """下划线字段是内部用的，不能漏给前端。"""
        spec = video._core_terms("二叉树遍历")
        cands = [{"title": "二叉树的遍历", "play": 100, "duration": "15:00",
                  "bvid": "BV9", "_secs": 900, "_danmaku": 5}]
        out = video._pick(cands, spec, 1)
        self.assertTrue(out)
        for f in ("_score", "_secs", "_danmaku"):
            self.assertNotIn(f, out[0])
        self.assertIn("_secs", cands[0], "原对象不能被改坏，否则下次读缓存少字段")

    def test_used_video_gets_deprioritized(self):
        """同一个视频在计划里出现两次可以，但不该每次都排第一。"""
        spec = video._core_terms("二叉树遍历")
        a = {"title": "二叉树的遍历 A", "play": 50000, "duration": "15:00",
             "bvid": "BVA", "_secs": 900, "_danmaku": 100}
        b = {"title": "二叉树的遍历 B", "play": 40000, "duration": "16:00",
             "bvid": "BVB", "_secs": 960, "_danmaku": 80}
        first = video._pick([a, b], spec, 1, used=set())
        self.assertEqual(first[0]["bvid"], "BVA")
        second = video._pick([a, b], spec, 1, used={"BVA"})
        self.assertEqual(second[0]["bvid"], "BVB", "推过的要让路")


class TargetTest(unittest.TestCase):
    def test_only_intensive_and_standard(self):
        """速览的内容不该配视频：为 5 分钟翻过去的点看 20 分钟视频是负收益。"""
        plan = {"days": [{"items": [
            {"name": "A", "level": "精学", "difficulty": 5, "exam_freq": 5, "importance": 5},
            {"name": "B", "level": "速览", "difficulty": 3, "exam_freq": 3, "importance": 3},
            {"name": "C", "level": "标准", "difficulty": 4, "exam_freq": 4, "importance": 4},
        ]}]}
        names = [it["name"] for it in video._pick_targets(plan, 8)]
        self.assertEqual(names, ["A", "C"])

    def test_hard_points_first(self):
        plan = {"days": [{"items": [
            {"name": "易", "level": "精学", "difficulty": 2, "exam_freq": 3, "importance": 3},
            {"name": "难", "level": "精学", "difficulty": 5, "exam_freq": 3, "importance": 3},
        ]}]}
        self.assertEqual(video._pick_targets(plan, 1)[0]["name"], "难",
                         "最需要有人讲的应该优先占名额")


class QueryTest(unittest.TestCase):
    def test_brackets_are_flattened(self):
        """中文平台对全角括号召回很差，必须拆成空格。"""
        q = video._build_query("数据结构", "快速傅里叶变换(FFT)")
        self.assertNotIn("(", q)
        self.assertIn("FFT", q)
        self.assertTrue(q.startswith("数据结构"))

    def test_length_capped(self):
        q = video._build_query("很长的课程名称" * 5, "很长很长的知识点名称" * 5)
        self.assertLessEqual(len(q), 50)


if __name__ == "__main__":
    unittest.main(verbosity=2)
