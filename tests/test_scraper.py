import os
import tempfile
import unittest
from unittest import mock

import pandas as pd
from bs4 import BeautifulSoup

import scraper


class ScraperTests(unittest.TestCase):
    def test_high_confidence_title_rules_cover_real_information_projects(self):
        self.assertTrue(scraper._has_strong_it_title_evidence(
            "灵丘县科技治超信息管理指挥系统建设项目"
        ))
        self.assertTrue(scraper._has_strong_it_title_evidence(
            "吉县教育系统网络运行维护费采购项目"
        ))
        self.assertTrue(scraper._has_strong_it_title_evidence(
            "山西省十五五数字孪生水利建设实施方案"
        ))
        self.assertTrue(scraper._has_strong_it_title_evidence(
            "基于DeepSeek智慧政务平台建设项目"
        ))

    def test_ambiguous_platform_word_is_not_a_direct_match(self):
        self.assertFalse(scraper._has_strong_it_title_evidence(
            "农产品交易平台建设配套道路工程"
        ))

    def test_detail_review_requires_title_and_requirement_evidence(self):
        self.assertTrue(scraper._should_review_with_details(
            "智能车管所设备采购项目", 0.55,
            "建设业务管理平台，配置服务器、数据库和网络设备。",
        ))
        self.assertFalse(scraper._should_review_with_details(
            "医疗设备更新采购项目", 0.55,
            "设备配套影像质控软件一套。",
        ))
        self.assertTrue(scraper._should_review_with_details(
            "智能设备采购项目", 0.55,
            "设备通过软件完成参数配置。",
        ))
        self.assertFalse(scraper._should_review_with_details(
            "消防监控值班服务项目", 0.49,
            "负责消防监控系统值班和日常巡查。",
        ))

    def test_borderline_project_can_be_promoted_by_detail_review(self):
        items = [
            {
                "标题": "智能车管所设备采购项目",
                "采购需求": "建设业务管理平台，配置服务器、数据库和网络设备。",
            },
            {
                "标题": "医疗设备更新采购项目",
                "采购需求": "设备配套影像质控软件一套。",
            },
        ]
        with mock.patch.object(
            scraper, "_encode_semantic_scores", side_effect=[[0.55, 0.55], [0.63]]
        ):
            scraper.classify_information_projects(items, object(), object())

        self.assertEqual(items[0]["是否信息化"], "是")
        self.assertEqual(items[0]["语义匹配度"], 0.63)
        self.assertEqual(items[1]["是否信息化"], "否")

    def test_borderline_semantic_score_needs_it_evidence(self):
        items = [{
            "标题": "文化遗产专业教学实验室建设项目",
            "采购需求": "建设文化遗产专业教学实验室。",
        }, {
            "标题": "智能交通监控设备采购项目",
            "采购需求": "详见采购文件。",
        }]
        with mock.patch.object(
            scraper, "_encode_semantic_scores", return_value=[0.75, 0.72]
        ):
            scraper.classify_information_projects(items, object(), object())

        self.assertEqual(items[0]["是否信息化"], "否")
        self.assertEqual(items[1]["是否信息化"], "是")

    def test_network_failure_stops_each_keyword_on_first_page(self):
        calls = []

        def failed_fetch(*args, **kwargs):
            calls.append(kwargs.get("params", {}).get("page_index"))
            return None, "network_error"

        with mock.patch.object(scraper, "fetch_page", side_effect=failed_fetch), \
             mock.patch.object(scraper.time, "sleep"), \
             mock.patch.object(scraper, "MAX_PAGES", 5):
            result = scraper.run_scraper_for_date("2026年07月13日")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(page == "1" for page in calls))

    def test_semantic_failure_does_not_create_result_file(self):
        search_html = """
        <html><body><ul class="vT-srch-result-list-bid"><li>
          <a href="http://example.test/project">信息化平台采购公告</a>
          <span>2026.07.12</span>
        </li></ul></body></html>
        """
        with tempfile.TemporaryDirectory() as output_dir, \
             mock.patch.object(scraper, "OUTPUT_DIR", output_dir), \
             mock.patch.object(scraper, "MAX_PAGES", 1), \
             mock.patch.object(scraper.time, "sleep"), \
             mock.patch.object(scraper, "fetch_page", return_value=(search_html, "ok")), \
             mock.patch.object(scraper, "validate_semantic_runtime", side_effect=RuntimeError("model unavailable")):
            result = scraper.run_scraper_for_date("2026年07月13日")

            self.assertEqual(result["status"], "failed")
            self.assertIsNone(result["file"])
            self.assertEqual(os.listdir(output_dir), [])

    def test_correction_backfill_accepts_html_string(self):
        correction_html = """
        <html><head><title>某项目更正公告</title></head><body>
          <a href="http://example.test/original">原公告地址</a>
        </body></html>
        """
        original_html = """
        <html><head><title>某项目竞争性磋商采购公告</title></head><body></body></html>
        """
        with mock.patch.object(scraper, "fetch_page", return_value=original_html):
            details = scraper.parse_project_details(correction_html)

        self.assertEqual(details["采购方式"], "竞争性磋商")

    def test_publish_date_is_extracted_from_search_item(self):
        soup = BeautifulSoup("<li>发布时间：2026.7.12</li>", "html.parser")
        self.assertEqual(scraper._extract_publish_date(soup.li), "2026-07-12")

    def test_result_validation_requires_semantic_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            invalid_path = os.path.join(temp_dir, "invalid.xlsx")
            pd.DataFrame([{"标题": "项目", "链接": "http://example.test"}]).to_excel(
                invalid_path, index=False
            )
            valid, reason = scraper.validate_result_file(invalid_path)

        self.assertFalse(valid)
        self.assertIn("是否信息化", reason)

    def test_valid_result_file_passes_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            valid_path = os.path.join(temp_dir, "valid.xlsx")
            pd.DataFrame(
                [{
                    "标题": "项目", "是否信息化": "是", "语义匹配度": 1.0,
                    "开标具体时间": "09:00", "开标地点": "太原", "链接": "http://example.test",
                }]
            ).to_excel(valid_path, index=False)
            valid, reason = scraper.validate_result_file(valid_path)

        self.assertTrue(valid, reason)

    def test_partial_result_is_retried_by_scheduler_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "partial.xlsx")
            frame = pd.DataFrame(
                [{
                    "标题": "项目", "是否信息化": "否", "语义匹配度": 0.3,
                    "开标具体时间": "09:00", "开标地点": "太原", "链接": "http://example.test",
                }]
            )
            with pd.ExcelWriter(path, engine="openpyxl") as writer:
                frame.to_excel(writer, index=False, sheet_name="山西信息化项目")
                pd.DataFrame([{"status": "partial"}]).to_excel(
                    writer, index=False, sheet_name="采集元数据"
                )
            valid, reason = scraper.validate_result_file(path)
            save_valid, save_reason = scraper.validate_result_file(path, require_complete=False)

        self.assertFalse(valid)
        self.assertIn("partial", reason)
        self.assertTrue(save_valid, save_reason)

    def test_detail_cache_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
             mock.patch.object(scraper, "DETAIL_CACHE_DB", os.path.join(temp_dir, "cache.db")), \
             mock.patch.object(scraper, "_cache_initialized", False):
            scraper._cache_details(
                "http://example.test/project", {"采购人名称": "测试单位"}, "<html>ok</html>"
            )
            cached = scraper._get_cached_details("http://example.test/project")

        self.assertEqual(cached["采购人名称"], "测试单位")


if __name__ == "__main__":
    unittest.main()
