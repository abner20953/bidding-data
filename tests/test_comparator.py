import os
import tempfile
import unittest
from unittest import mock

import fitz

from dashboard.utils import comparator
from dashboard.utils.comparator import CollusionDetector, compare_documents


def create_pdf(path, pages, metadata=None):
    document = fitz.open()
    for content in pages:
        page = document.new_page()
        page.insert_text((72, 72), content, fontsize=10)
    if metadata:
        document.set_metadata(metadata)
    document.save(path)
    document.close()


class ComparatorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_dir = os.path.join(self.temp_dir.name, "cache")
        self.cache_patch = mock.patch.object(comparator, "CACHE_DIR", self.cache_dir)
        self.cache_patch.start()

    def tearDown(self):
        self.cache_patch.stop()
        self.temp_dir.cleanup()

    def path(self, name):
        return os.path.join(self.temp_dir.name, name)

    def test_entity_extraction_keeps_full_valid_identity(self):
        detector = CollusionDetector()
        entities = detector.extract_entities(
            "identity 11010519491231002X mobile 13800138000 email TEST@example.com"
        )
        self.assertIn("11010519491231002X", entities)
        self.assertIn("13800138000", entities)
        self.assertIn("test@example.com", entities)
        self.assertNotIn("110105194912310", entities)

    def test_pdf_page_limits_allow_2000_pages_per_file(self):
        self.assertEqual(comparator.MAX_PDF_PAGES, 2000)
        comparator._validate_total_page_budget((2000, 2000))

    def test_total_pdf_page_limit_rejects_4001_pages(self):
        with self.assertRaisesRegex(comparator.ComparisonLimitError, "4000"):
            comparator._validate_total_page_budget((2000, 2000, 1))

    def test_total_character_limit_rejects_oversized_comparison(self):
        with self.assertRaisesRegex(comparator.ComparisonLimitError, "12,000,000"):
            comparator._validate_total_character_budget((4_000_000, 4_000_000, 4_000_001))

    def test_adjacent_entities_separated_only_by_space(self):
        detector = CollusionDetector()
        entities = detector.extract_entities("11010519491231002X 13800138000")

        self.assertIn("11010519491231002X", entities)
        self.assertIn("13800138000", entities)

    def test_tender_copy_with_presentation_punctuation_difference_is_suppressed(self):
        detector = CollusionDetector()
        detector.tender_path = "mock-tender.pdf"
        raw_tender_text = (
            "供应商应建立（质量复核）制度，并保存全过程记录。"
        )
        pages = [(1, raw_tender_text, detector.normalize(raw_tender_text))]
        with mock.patch.object(
            detector,
            "extract_text_with_pages",
            return_value=(
                raw_tender_text,
                pages,
                {},
                {"extracted_chars": len(raw_tender_text)},
            ),
        ):
            detector.load_tender()

        bid_text = detector.normalize(
            "供应商应建立质量复核制度:并保存全过程记录"
        )

        self.assertTrue(detector._is_tender_copy(bid_text))

    def test_tender_source_canonical_keeps_decimal_requirement_significant(self):
        detector = CollusionDetector()
        tender_text = detector.normalize("桌面厚度为1.5mm并满足承重要求")
        detector.tender_full_text = tender_text
        canonical = detector.tender_source_canonical(tender_text)
        detector.tender_source_hashes = {
            detector._tender_source_digest(canonical)
        }

        bid_text = detector.normalize("桌面厚度为15mm并满足承重要求")

        self.assertFalse(detector._is_tender_copy(bid_text))

    def test_tender_placeholder_requires_value_verified_in_tender(self):
        detector = CollusionDetector()
        detector.tender_full_text = detector.normalize(
            "项目编号：（在此填写项目编号）"
        )
        detector.tender_field_templates = {
            detector._tender_field_signature("项目编号：（在此填写项目编号）")
        }
        detector.tender_field_values = {"项目编号": {"f19-2026-001"}}

        self.assertTrue(
            detector._is_tender_copy(detector.normalize("项目编号：F19-2026-001"))
        )
        self.assertFalse(
            detector._is_tender_copy(detector.normalize("项目编号：F19-2026-002"))
        )
        for truncated in ("F19-2026", "2026-001", "2026"):
            self.assertFalse(
                detector._is_tender_copy(
                    detector.normalize(f"项目编号：{truncated}")
                )
            )
        self.assertFalse(
            detector._is_tender_copy(detector.normalize("质保期：3年"))
        )

    def test_tender_source_canonical_keeps_comparison_operators_significant(self):
        detector = CollusionDetector()
        tender_text = detector.normalize("设备运行噪声<50db并满足验收要求")
        detector.tender_full_text = tender_text
        canonical = detector.tender_source_canonical(tender_text)
        detector.tender_source_hashes = {
            detector._tender_source_digest(canonical)
        }

        changed_text = detector.normalize("设备运行噪声>50db并满足验收要求")

        self.assertFalse(detector._is_tender_copy(changed_text))
        self.assertIn("<", detector.tender_source_canonical(tender_text))
        self.assertIn(">", detector.tender_source_canonical(changed_text))
        self.assertNotEqual(
            detector.tender_source_canonical("接口状态!=0时应立即告警"),
            detector.tender_source_canonical("接口状态=0时应立即告警"),
        )

    def test_real_tender_loading_keeps_not_equal_operator_significant(self):
        detector = CollusionDetector()
        detector.tender_path = "mock-tender.pdf"
        raw_text = "接口状态!=0时应立即告警并记录全部异常状态信息"
        pages = [(1, raw_text, detector.normalize(raw_text))]

        with mock.patch.object(
            detector,
            "extract_text_with_pages",
            return_value=(raw_text, pages, {}, {"extracted_chars": len(raw_text)}),
        ):
            detector.load_tender()

        changed_text = detector.normalize(
            "接口状态=0时应立即告警并记录全部异常状态信息"
        )
        self.assertFalse(detector._is_tender_copy(changed_text))

    def test_real_tender_loading_keeps_standalone_logical_not_significant(self):
        detector = CollusionDetector()
        detector.tender_path = "mock-tender.pdf"
        raw_text = "system must verify !ready status before continuing operation"
        pages = [(1, raw_text, detector.normalize(raw_text))]

        with mock.patch.object(
            detector,
            "extract_text_with_pages",
            return_value=(raw_text, pages, {}, {"extracted_chars": len(raw_text)}),
        ):
            detector.load_tender()

        changed_text = detector.normalize(
            "system must verify ready status before continuing operation"
        )
        self.assertFalse(detector._is_tender_copy(changed_text))

    def test_tender_source_canonical_keeps_numeric_ratio_colon(self):
        detector = CollusionDetector()

        self.assertNotEqual(
            detector.tender_source_canonical("材料配比为1:2并充分搅拌"),
            detector.tender_source_canonical("材料配比为12并充分搅拌"),
        )

    def test_tender_field_signature_rejects_free_form_or_merged_text(self):
        detector = CollusionDetector()

        self.assertEqual(
            detector._tender_field_signature(
                "项目名称、关联地图底图、配置项目基础属性信息"
            ),
            "",
        )
        self.assertEqual(
            detector._tender_field_signature(
                "包号：以本公司名义处理一切与之有关的事务"
            ),
            "",
        )
        self.assertEqual(
            detector._tender_field_signature("日期间应完成全部现场复核工作"),
            "",
        )
        self.assertEqual(
            detector._tender_field_signature(
                "项目编号：项目编号生成规则应与内部系统保持一致"
            ),
            "",
        )
        self.assertEqual(
            detector._tender_field_signature("项目编号：1404992026CGK00239"),
            "项目编号",
        )

    def test_concrete_tender_field_change_is_not_suppressed(self):
        detector = CollusionDetector()
        detector.tender_path = "mock-tender.pdf"
        raw_text = (
            "项目编号：（在此填写项目编号）\n"
            "项目编号：A-001-2026"
        )
        pages = [(1, raw_text, detector.normalize(raw_text))]

        with mock.patch.object(
            detector,
            "extract_text_with_pages",
            return_value=(raw_text, pages, {}, {"extracted_chars": len(raw_text)}),
        ):
            detector.load_tender()

        self.assertIn("项目编号", detector.tender_field_templates)
        self.assertEqual(
            detector.tender_field_values.get("项目编号"), {"a-001-2026"}
        )
        self.assertFalse(
            detector._is_tender_copy(
                detector.normalize("项目编号：A-002-2026")
            )
        )
        for truncated in ("A-001", "001-2026", "2026"):
            self.assertFalse(
                detector._is_tender_copy(
                    detector.normalize(f"项目编号：{truncated}")
                )
            )
        self.assertTrue(
            detector._is_tender_copy(
                detector.normalize("项目编号：A-001-2026")
            )
        )

    def test_tender_colon_boundary_indexes_bid_comma_fragment(self):
        detector = CollusionDetector()
        detector.tender_path = "mock-tender.pdf"
        raw_text = "供应商应建立完善质量复核管理制度:并保存完整的全过程复核记录"
        pages = [(1, raw_text, detector.normalize(raw_text))]

        with mock.patch.object(
            detector,
            "extract_text_with_pages",
            return_value=(raw_text, pages, {}, {"extracted_chars": len(raw_text)}),
        ):
            detector.load_tender()

        fragments = (
            "供应商应建立完善质量复核管理制度",
            "并保存完整的全过程复核记录",
        )
        for fragment in fragments:
            canonical = detector.tender_source_canonical(fragment)
            self.assertIn(
                detector._tender_source_digest(canonical),
                detector.tender_source_hashes,
            )

    def test_tender_source_hashes_do_not_bridge_dropped_short_requirement(self):
        detector = CollusionDetector()
        detector.tender_path = "mock-tender.pdf"
        first = "供应商应建立完善质量复核管理制度"
        second = "并保存完整的全过程复核记录"
        raw_text = f"{first}。不得。{second}。"
        pages = [(1, raw_text, detector.normalize(raw_text))]

        with mock.patch.object(
            detector,
            "extract_text_with_pages",
            return_value=(raw_text, pages, {}, {"extracted_chars": len(raw_text)}),
        ):
            detector.load_tender()

        deleted_requirement = detector.normalize(f"{first}:{second}")
        self.assertFalse(detector._is_tender_copy(deleted_requirement))

    def test_load_tender_builds_bounded_canonical_digest_index(self):
        detector = CollusionDetector()
        detector.tender_path = "mock-tender.pdf"
        raw_text = (
            "项目编号：（在此填写项目编号）\n"
            "日期：（年月日）\n"
            "供应商应建立（质量复核）制度，并保存全过程记录。"
        )
        pages = [(1, raw_text, detector.normalize(raw_text))]

        with mock.patch.object(
            detector,
            "extract_text_with_pages",
            return_value=(raw_text, pages, {}, {"extracted_chars": len(raw_text)}),
        ):
            detector.load_tender()

        self.assertIn("项目编号", detector.tender_field_templates)
        self.assertNotIn("日期", detector.tender_field_templates)
        self.assertTrue(detector.tender_source_hashes)
        self.assertTrue(
            all(
                isinstance(digest, bytes) and len(digest) == 16
                for digest in detector.tender_source_hashes
            )
        )
        self.assertFalse(hasattr(detector, "tender_full_canonical"))

    def test_metadata_auxiliary_is_separate_and_marks_tender_common_value(self):
        detector = CollusionDetector()

        auxiliary = detector._build_metadata_auxiliary(
            {
                "author": "Review User",
                "creator": "Office Suite",
                "producer": "PDF Engine",
            },
            {
                "author": "review user",
                "creator": "Office Suite",
                "producer": "PDF Engine",
            },
            {"producer": "PDF Engine"},
        )

        matches = {item["field"]: item for item in auxiliary["matches"]}
        self.assertEqual(matches["author"]["strength"], "reference")
        self.assertEqual(matches["creator"]["strength"], "weak")
        self.assertTrue(matches["producer"]["also_in_tender"])
        self.assertIn("不参与相似度分数", auxiliary["notice"])

        invalid_auxiliary = detector._build_metadata_auxiliary(
            {"author": "Unknown", "title": "N/A"},
            {"author": "unknown", "title": "n/a"},
        )
        self.assertFalse(invalid_auxiliary["matches"])

    def test_matching_metadata_does_not_create_collision_or_change_legacy_fields(self):
        path_a = self.path("a.pdf")
        path_b = self.path("b.pdf")
        shared_metadata = {"author": "Same Author", "creator": "Same Office"}
        create_pdf(path_a, ["Only file A content is present."], shared_metadata)
        create_pdf(path_b, ["Only file B content is present."], shared_metadata)

        result = compare_documents(
            path_a, path_b, check_entity=False, check_text=False, check_spelling=False
        )

        self.assertFalse(result["paragraphs"])
        self.assertEqual(result["summary"]["total"], 0)
        self.assertEqual(result["metadata"]["file_a"]["author"], "Same Author")
        matched_fields = {
            item["field"] for item in result["metadata"]["auxiliary"]["matches"]
        }
        self.assertIn("author", matched_fields)

    def test_fuzzy_match_detects_small_rewrite(self):
        path_a = self.path("a.pdf")
        path_b = self.path("b.pdf")
        create_pdf(
            path_a,
            ["The project implementation schedule includes three quality review stages."],
        )
        create_pdf(
            path_b,
            ["The project implementation schedule contains three quality review stages."],
        )

        result = compare_documents(path_a, path_b, check_entity=False)

        fuzzy = [item for item in result["paragraphs"] if item["type"] == "fuzzy"]
        self.assertTrue(fuzzy)
        self.assertGreaterEqual(fuzzy[0]["similarity"], 78)

    def test_standalone_short_exact_fragment_is_not_reported(self):
        path_a = self.path("a.pdf")
        path_b = self.path("b.pdf")
        create_pdf(path_a, ["支持接口调用"])
        create_pdf(path_b, ["支持接口调用"])

        result = compare_documents(path_a, path_b, check_entity=False)

        self.assertFalse(
            [item for item in result["paragraphs"] if item["type"] == "text"]
        )

    def test_tender_table_title_moved_to_paragraph_start_is_not_an_edit(self):
        detector = CollusionDetector()
        tender_text = detector.normalize("提供测试数据源连接情况的能力")
        bid_text = detector.normalize("数据源连接测试提供测试数据源连接情况的能力")

        evidence = detector._shared_tender_edit_evidence(
            tender_text, bid_text, bid_text
        )

        self.assertIsNone(evidence)

    def test_bid_form_boilerplate_is_suppressed(self):
        detector = CollusionDetector()
        exact_units = [
            {"text": detector.normalize("邮政编码:030041"), "page": 1, "order": 0},
            {
                "text": detector.normalize("项目生产采用专用的五级质量复检程序"),
                "page": 2,
                "order": 1,
            },
        ]

        exact, _ = detector._find_exact_collisions(exact_units, exact_units)

        self.assertEqual(len(exact), 1)
        self.assertIn("五级质量复检程序", exact[0]["text_a"])

        fuzzy = detector._find_fuzzy_collisions(
            [
                {
                    "text": detector.normalize(
                        "投标人:甲公司(电子签章)法定代表人或其委托代理人:"
                        "(电子签章)2026年7月12日"
                    ),
                    "page": 1,
                }
            ],
            [
                {
                    "text": detector.normalize(
                        "投标人:乙公司(电子签章)法定代表人或其委托代理人:"
                        "(电子签章)2026年7月12日"
                    ),
                    "page": 1,
                }
            ],
            set(),
        )
        self.assertFalse(fuzzy)

    def test_page_counter_lines_are_removed_from_fuzzy_units(self):
        detector = CollusionDetector()
        content = (
            "项目实施方案包含质量复检现场验收进度控制和售后响应程序\n"
            "72\n56/193"
        )
        pages = [(56, content, detector.normalize(content))]

        units = detector.get_comparison_units(pages)

        self.assertTrue(units)
        self.assertTrue(all("56/193" not in unit["text"] for unit in units))
        self.assertTrue(all(not unit["text"].endswith("72") for unit in units))

    def test_business_materials_outline_is_suppressed(self):
        detector = CollusionDetector()

        self.assertTrue(
            detector._is_low_value_boilerplate(
                "八、商务部分资料1、附2025年度审计报告、获奖情况及"
                "能证明企业实力的资料"
            )
        )

    def test_tender_derived_parameter_table_is_suppressed(self):
        detector = CollusionDetector()
        text_a = detector.normalize(
            "序号1办公桌2200*2050*760mm厚度66mm密度26kg/m3承重102kg"
            "耐磨80000次采用水性环保油漆和三节导轨"
        )
        text_b = detector.normalize(
            "序号1主管桌2200*2050*760mm厚度66mm密度26kg/m3承重102kg"
            "耐磨80000次采用水性环保油漆和三节导轨"
        )

        with mock.patch.object(
            detector, "_tender_shingle_coverage", return_value=0.8
        ), mock.patch.object(
            detector, "_shared_nontender_shingle_stats", return_value=(8, 0.1)
        ):
            fuzzy = detector._find_fuzzy_collisions(
                [{"text": text_a, "page": 10}],
                [{"text": text_b, "page": 20}],
                set(),
            )

        self.assertFalse(fuzzy)

    def test_parameter_table_keeps_substantial_shared_nontender_content(self):
        detector = CollusionDetector()
        shared_custom_text = (
            "双方共同新增现场复核流程并要求每批产品记录责任人和复核时间"
        )
        text_a = detector.normalize(
            "序号1办公桌2200*2050*760mm厚度66mm密度26kg/m3承重102kg"
            "耐磨80000次" + shared_custom_text
        )
        text_b = detector.normalize(
            "序号1主管桌2200*2050*760mm厚度66mm密度26kg/m3承重102kg"
            "耐磨80000次" + shared_custom_text
        )

        with mock.patch.object(
            detector, "_tender_shingle_coverage", return_value=0.8
        ), mock.patch.object(
            detector, "_shared_nontender_shingle_stats", return_value=(36, 0.3)
        ):
            fuzzy = detector._find_fuzzy_collisions(
                [{"text": text_a, "page": 10}],
                [{"text": text_b, "page": 20}],
                set(),
            )

        self.assertTrue(fuzzy)

    def test_fuzzy_match_covered_by_exact_substring_is_suppressed(self):
        detector = CollusionDetector()
        first_exact = detector.normalize("施工队伍进场后参加安全教育培训")
        second_exact = detector.normalize("参加考试不合格的施工人员清退出场")
        combined = first_exact + second_exact

        fuzzy = detector._find_fuzzy_collisions(
            [{"text": combined, "page": 10}],
            [{"text": detector.normalize("安全施工注意事项") + combined, "page": 20}],
            {first_exact, second_exact},
        )

        self.assertFalse(fuzzy)

    def test_nearby_exact_segments_merge_when_three_or_more_align(self):
        detector = CollusionDetector()
        texts = [
            detector.normalize("第一项共同生产质量检验控制程序"),
            detector.normalize("第二项共同生产复检控制程序"),
            detector.normalize("第三项共同生产问题上报程序"),
        ]
        units_a = [
            {"text": text, "page": 1, "order": order}
            for text, order in zip(texts, (0, 2, 4))
        ]
        units_b = [
            {"text": text, "page": 3, "order": order}
            for text, order in zip(texts, (0, 2, 4))
        ]

        exact, _ = detector._find_exact_collisions(units_a, units_b)

        self.assertEqual(len(exact), 1)
        self.assertEqual(exact[0]["segment_count"], 3)

    def test_shared_tender_edit_has_separate_classification(self):
        path_a = self.path("a.pdf")
        path_b = self.path("b.pdf")
        tender_path = self.path("tender.pdf")
        create_pdf(path_a, ["The supplier shall provide a six year warranty for all devices."])
        create_pdf(path_b, ["The supplier shall provide a six year warranty for all devices."])
        create_pdf(tender_path, ["The supplier shall provide a five year warranty for all devices."])

        result = compare_documents(
            path_a, path_b, tender_path, check_entity=False, check_spelling=False
        )

        tender_related = [
            item for item in result["paragraphs"] if item["type"] == "tender_related"
        ]
        self.assertTrue(tender_related)
        self.assertTrue(tender_related[0]["tender_text"])
        self.assertTrue(tender_related[0]["shared_edits"])

    def test_tender_copy_in_one_file_is_not_a_shared_edit(self):
        detector = CollusionDetector()
        tender_text = "thesuppliershallprovideafiveyearwarrantyforallproducts"
        text_a = "thesuppliershallprovideafouryearwarrantyforallproducts"

        evidence = detector._shared_tender_edit_evidence(
            tender_text, text_a, tender_text
        )

        self.assertIsNone(evidence)

    def test_different_tender_edits_are_not_shared(self):
        detector = CollusionDetector()
        tender_text = "thesuppliershallprovideafiveyearwarrantyandannualsupport"
        text_a = "thesuppliershallprovideasixyearwarrantyandannualsupport"
        text_b = "thesuppliershallprovideafiveyearwarrantyandenhancedsupport"

        evidence = detector._shared_tender_edit_evidence(
            tender_text, text_a, text_b
        )

        self.assertIsNone(evidence)

    def test_shared_minor_edit_does_not_hide_major_independent_edit(self):
        detector = CollusionDetector()
        tender_text = "thesuppliershallprovideafiveyearwarrantyandannualsupport"
        text_a = "thesuppliershallprovideasixyearwarrantyandannualsupport"
        text_b = (
            "thesuppliershallprovideasixyearwarrantyandquarterlyaudited"
            "enhancedsupportwithonsiteservice"
        )

        evidence = detector._shared_tender_edit_evidence(
            tender_text, text_a, text_b
        )

        self.assertIsNone(evidence)

    def test_table_cell_reordering_is_not_a_shared_tender_edit(self):
        detector = CollusionDetector()
        tender_text = (
            "会议椅22把172套材质说明厚度25mm密度26kg/m3承重102kg"
            "耐磨80000次钢管直径32mm壁厚1.5mm"
        )
        reordered = (
            "22会议椅材质说明厚度25mm密度26kg/m3承重102kg"
            "耐磨80000次钢管直径32mm壁厚1.5mm"
        )

        evidence = detector._shared_tender_edit_evidence(
            tender_text, reordered, reordered
        )

        self.assertIsNone(evidence)

    def test_numeric_only_tender_change_is_preserved(self):
        detector = CollusionDetector()
        tender_text = detector.normalize(
            "本项目所有家具质保期为5年并要求供应商在24小时内响应服务"
        )
        bid_text = detector.normalize(
            "本项目所有家具质保期为3年并要求供应商在12小时内响应服务"
        )
        detector.tender_full_text = tender_text
        detector.tender_exact_texts = {tender_text}
        detector.tender_units = [{"text": tender_text, "page": 1}]
        detector.tender_unit_index = detector._build_unit_index(detector.tender_units)
        units = [{"text": bid_text, "page": 2, "order": 0}]

        collisions, _ = detector._find_exact_collisions(units, units)

        self.assertEqual(len(collisions), 1)
        self.assertEqual(collisions[0]["type"], "tender_related")
        self.assertIn(
            {"original": "5", "modified": "3"}, collisions[0]["shared_edits"]
        )

    def test_short_fragmented_tender_table_copy_is_suppressed(self):
        detector = CollusionDetector()
        tender_text = detector.normalize(
            "22会议椅产品规格列常规数量448把材质说明"
            "1)皮革优质2)椅身:裁切棉密度26kg"
        )
        bid_text = detector.normalize("22会议椅常规2)椅身:裁切棉")
        detector.tender_full_text = tender_text
        detector.tender_exact_texts = {tender_text}
        detector.tender_units = [{"text": tender_text, "page": 1}]
        detector.tender_unit_index = detector._build_unit_index(detector.tender_units)
        units = [{"text": bid_text, "page": 2, "order": 0}]

        collisions, _ = detector._find_exact_collisions(units, units)

        self.assertFalse(collisions)

    def test_unverified_tender_derived_fuzzy_text_is_suppressed(self):
        detector = CollusionDetector()
        tender_text = (
            "thesuppliershallprovideafiveyearwarrantyandannualonsitesupport"
            "forallofficefurnitureproducts"
        )
        text_a = tender_text.replace("five", "six")
        text_b = tender_text.replace("annual", "quarterly")
        detector.tender_units = [{"text": tender_text, "page": 1}]
        detector.tender_unit_index = detector._build_unit_index(detector.tender_units)

        matches = detector._find_fuzzy_collisions(
            [{"text": text_a, "page": 2}],
            [{"text": text_b, "page": 3}],
            set(),
        )

        self.assertFalse(matches)

    def test_tender_derived_table_reordering_is_suppressed(self):
        detector = CollusionDetector()
        tender_text = (
            "会议椅22把172套材质说明厚度25mm密度26kg/m3承重102kg"
            "耐磨80000次钢管直径32mm壁厚1.5mm表面静电喷涂"
        )
        text_a = (
            "22会议椅材质说明厚度25mm密度26kg/m3承重102kg"
            "耐磨80000次钢管直径32mm壁厚1.5mm表面静电喷涂"
        )
        text_b = (
            "会议椅22材质说明厚度25mm密度26kg/m3承重102kg"
            "耐磨80000次钢管直径32mm壁厚1.5mm表面静电粉末喷涂"
        )
        detector.tender_units = [{"text": tender_text, "page": 1}]
        detector.tender_unit_index = detector._build_unit_index(detector.tender_units)

        matches = detector._find_fuzzy_collisions(
            [{"text": text_a, "page": 2}],
            [{"text": text_b, "page": 3}],
            set(),
        )

        self.assertFalse(matches)

    def test_contiguous_exact_segments_are_merged(self):
        path_a = self.path("a.pdf")
        path_b = self.path("b.pdf")
        shared_lines = [
            "First unique shared implementation requirement",
            "Second unique shared implementation requirement",
            "Third unique shared implementation requirement",
        ]
        create_pdf(path_a, ["\n".join(shared_lines)])
        create_pdf(path_b, ["\n".join(shared_lines)])

        result = compare_documents(path_a, path_b, check_entity=False)

        exact = [item for item in result["paragraphs"] if item["type"] == "text"]
        self.assertEqual(len(exact), 1)
        self.assertEqual(exact[0]["segment_count"], 3)
        self.assertEqual(exact[0]["page_a"], 1)
        self.assertEqual(exact[0]["page_a_end"], 1)

    def test_exact_segments_are_not_merged_across_different_content(self):
        path_a = self.path("a.pdf")
        path_b = self.path("b.pdf")
        create_pdf(
            path_a,
            [
                "First unique shared implementation requirement\n"
                "Only file A has this intervening requirement\n"
                "Second unique shared implementation requirement"
            ],
        )
        create_pdf(
            path_b,
            [
                "First unique shared implementation requirement\n"
                "Only file B has this intervening requirement\n"
                "Second unique shared implementation requirement"
            ],
        )

        result = compare_documents(path_a, path_b, check_entity=False)

        exact = [item for item in result["paragraphs"] if item["type"] == "text"]
        self.assertEqual(len(exact), 2)
        self.assertTrue(all(item["segment_count"] == 1 for item in exact))

    def test_exact_segments_in_different_section_order_are_preserved(self):
        path_a = self.path("a.pdf")
        path_b = self.path("b.pdf")
        first = "First unique shared implementation requirement"
        second = "Second unique shared implementation requirement"
        create_pdf(path_a, [f"{first}\n{second}"])
        create_pdf(path_b, [f"{second}\n{first}"])

        result = compare_documents(path_a, path_b, check_entity=False)

        exact = [item for item in result["paragraphs"] if item["type"] == "text"]
        self.assertEqual(len(exact), 2)
        self.assertEqual(
            {item["text_a"] for item in exact},
            {CollusionDetector.normalize(first), CollusionDetector.normalize(second)},
        )

    def test_page_level_scan_statistics(self):
        path_a = self.path("a.pdf")
        path_b = self.path("b.pdf")
        create_pdf(path_a, ["", "", "readable ascii page"])
        create_pdf(path_b, ["", "readable ascii page"])

        result = compare_documents(path_a, path_b, check_entity=False, check_text=False)
        stats = result["metadata"]["text_stats"]

        self.assertEqual(stats["file_a"]["total_pages"], 3)
        self.assertEqual(stats["file_a"]["suspected_scan_pages"], 3)
        self.assertTrue(result["metadata"]["warnings"])

    def test_extraction_cache_is_reused(self):
        pdf_path = self.path("cached.pdf")
        create_pdf(pdf_path, ["cacheable document content"])
        detector = CollusionDetector()
        first = detector.extract_text_with_pages(pdf_path)

        with mock.patch("fitz.open", side_effect=AssertionError("PDF reopened")):
            second = detector.extract_text_with_pages(pdf_path)

        self.assertEqual(first, second)

    def test_fuzzy_matching_does_not_reuse_one_target_unit(self):
        detector = CollusionDetector()
        units_a = [
            {"text": "projectimplementationqualitycontrolschedulealpha", "page": 1},
            {"text": "projectimplementationqualitycontrolschedulebeta", "page": 2},
        ]
        units_b = [
            {"text": "projectimplementationsafetycontrolschedulealpha", "page": 3}
        ]

        matches = detector._find_fuzzy_collisions(units_a, units_b, set())

        self.assertLessEqual(len(matches), 1)

    def test_unit_index_uses_compact_signatures_and_caps_common_postings(self):
        detector = CollusionDetector()
        units = [
            {"text": f"sharedcomparisonprefix{index:04d}sharedcomparisonsuffix", "page": 1}
            for index in range(comparator.MAX_POSTINGS_PER_SHINGLE + 10)
        ]

        index = detector._build_unit_index(units)

        self.assertNotIn("signatures", index)
        self.assertEqual(len(index["signature_sizes"]), len(units))
        posting_lists = [
            value for value in index["postings"].values() if isinstance(value, list)
        ]
        self.assertTrue(posting_lists)
        self.assertLessEqual(
            max(len(value) for value in posting_lists),
            comparator.MAX_POSTINGS_PER_SHINGLE + 1,
        )

    def test_excessive_comparison_units_are_rejected_before_indexing(self):
        detector = CollusionDetector()
        content = (
            "First sufficiently long comparison segment,"
            "Second sufficiently long comparison segment"
        )
        pages = [(1, content, detector.normalize(content))]

        with mock.patch.object(comparator, "MAX_EXACT_UNITS_PER_FILE", 1):
            with self.assertRaisesRegex(comparator.ComparisonLimitError, "短段过多"):
                detector.get_exact_units(pages)

    def test_shared_repeated_punctuation_is_reported(self):
        path_a = self.path("a.pdf")
        path_b = self.path("b.pdf")
        content = "The submitted amount is valid,, please review the calculation."
        create_pdf(path_a, [content])
        create_pdf(path_b, [content])

        result = compare_documents(
            path_a,
            path_b,
            check_entity=False,
            check_text=True,
            check_spelling=True,
        )

        errors = [item for item in result["paragraphs"] if item["type"] == "shared_error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error_kind"], "punctuation")
        self.assertFalse(
            [item for item in result["paragraphs"] if item["type"] == "text"]
        )

    def test_error_in_tender_is_not_reported_as_shared_bid_error(self):
        path_a = self.path("a.pdf")
        path_b = self.path("b.pdf")
        tender_path = self.path("tender.pdf")
        content = "The submitted amount is valid,, please review the calculation."
        create_pdf(path_a, [content])
        create_pdf(path_b, [content])
        create_pdf(tender_path, [content])

        result = compare_documents(
            path_a,
            path_b,
            tender_path,
            check_entity=False,
            check_text=False,
            check_spelling=True,
        )

        errors = [item for item in result["paragraphs"] if item["type"] == "shared_error"]
        self.assertFalse(errors)

    def test_error_only_mode_does_not_build_text_indexes(self):
        path_a = self.path("a.pdf")
        path_b = self.path("b.pdf")
        tender_path = self.path("tender.pdf")
        content = "The submitted amount is wrong,, please review it."
        create_pdf(path_a, [content])
        create_pdf(path_b, [content])
        create_pdf(tender_path, ["Tender content without the punctuation issue."])

        with mock.patch.object(
            CollusionDetector,
            "_build_unit_index",
            side_effect=AssertionError("text index should not be built"),
        ):
            result = compare_documents(
                path_a,
                path_b,
                tender_path,
                check_entity=False,
                check_text=False,
                check_spelling=True,
            )

        self.assertEqual(result["summary"]["shared_error"], 1)

    def test_same_punctuation_pattern_in_different_context_is_not_reported(self):
        detector = CollusionDetector()
        pages_a = [(1, "Alpha contract amount is wrong,, review it", "")]
        pages_b = [(1, "Beta delivery schedule is wrong,, review it", "")]

        errors = detector._find_shared_high_confidence_errors(pages_a, pages_b)

        self.assertFalse(errors)

    def test_ellipsis_does_not_hide_later_punctuation_error(self):
        detector = CollusionDetector()
        content = "Wait... the submitted amount is wrong,, review it"
        pages = [(1, content, detector.normalize(content))]

        errors = detector._find_shared_high_confidence_errors(pages, pages)

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error_kind"], "punctuation")
        self.assertIn(",,", errors[0]["desc"])

    def test_shared_explicit_calculation_error_is_reported(self):
        detector = CollusionDetector()
        content = "Quantity calculation: 12 * 5 = 70"
        pages = [(1, content, detector.normalize(content))]

        errors = detector._find_shared_high_confidence_errors(pages, pages)

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error_kind"], "calculation")
        self.assertIn("60", errors[0]["desc"])

    def test_valid_explicit_calculation_is_not_reported(self):
        detector = CollusionDetector()
        content = "Quantity calculation: 12 * 5 = 60"
        pages = [(1, content, detector.normalize(content))]

        errors = detector._find_shared_high_confidence_errors(pages, pages)

        self.assertFalse(errors)

    def test_duplicate_number_in_established_list_is_reported(self):
        detector = CollusionDetector()
        content = "1. First requirement\n2. Second requirement\n2. Duplicate requirement\n3. Third requirement"
        pages = [(1, content, detector.normalize(content))]

        errors = detector._find_shared_high_confidence_errors(pages, pages)

        numbering = [item for item in errors if item["error_kind"] == "numbering"]
        self.assertEqual(len(numbering), 1)
        self.assertIn("重复", numbering[0]["desc"])

    def test_numbering_gap_alone_is_not_reported(self):
        detector = CollusionDetector()
        content = "1. First requirement\n2. Second requirement\n4. Fourth requirement"
        pages = [(1, content, detector.normalize(content))]

        errors = detector._find_shared_high_confidence_errors(pages, pages)

        self.assertFalse(errors)

    def test_new_parenthesized_sublist_start_is_not_duplicate_number(self):
        detector = CollusionDetector()
        content = (
            "1) Cable routing requirement\n"
            "1) Separate desk configuration\n"
            "2) Desktop material requirement"
        )
        pages = [(1, content, detector.normalize(content))]

        errors = detector._find_shared_high_confidence_errors(pages, pages)

        self.assertFalse(errors)

    def test_numbered_closing_parenthesis_is_not_unmatched_bracket(self):
        detector = CollusionDetector()
        content = "5）Bed board thickness requirement"
        pages = [(1, content, detector.normalize(content))]

        errors = detector._find_shared_high_confidence_errors(pages, pages)

        self.assertFalse(errors)

    def test_single_numbering_style_outlier_is_reported(self):
        detector = CollusionDetector()
        content = "1. First requirement\n2. Second requirement\n3、Third requirement\n4. Fourth requirement"
        pages = [(1, content, detector.normalize(content))]

        errors = detector._find_shared_high_confidence_errors(pages, pages)

        numbering = [item for item in errors if item["error_kind"] == "numbering"]
        self.assertEqual(len(numbering), 1)
        self.assertIn("主要使用", numbering[0]["desc"])

    def test_unknown_single_character_replacement_is_not_auto_classified_as_typo(self):
        detector = CollusionDetector()
        tender_text = detector.normalize("供应商应保证产品质量标准符合要求")
        bid_text = detector.normalize("供应商应保证产品质最标准符合要求")

        evidence = detector._shared_tender_edit_evidence(
            tender_text, bid_text, bid_text
        )

        self.assertTrue(evidence)
        self.assertNotIn("probable_typos", evidence)

    def test_known_word_replacement_is_not_treated_as_typo(self):
        detector = CollusionDetector()
        tender_text = detector.normalize("供应商应满足国家标准")
        bid_text = detector.normalize("供应商应符合国家标准")

        evidence = detector._shared_tender_edit_evidence(
            tender_text, bid_text, bid_text
        )

        self.assertTrue(evidence)
        self.assertNotIn("probable_typos", evidence)

    def test_legitimate_material_change_is_not_classified_as_shared_error(self):
        detector = CollusionDetector()
        tender_text = detector.normalize(
            "本项目所有家具均采用优质木质材料进行生产制作并满足环保标准要求"
        )
        bid_text = detector.normalize(
            "本项目所有家具均采用优质竹质材料进行生产制作并满足环保标准要求"
        )
        detector.tender_full_text = tender_text
        detector.tender_exact_texts = {tender_text}
        detector.tender_units = [{"text": tender_text, "page": 1}]
        detector.tender_unit_index = detector._build_unit_index(detector.tender_units)
        units = [{"text": bid_text, "page": 2, "order": 0}]

        collisions, _ = detector._find_exact_collisions(units, units)

        self.assertEqual(len(collisions), 1)
        self.assertEqual(collisions[0]["type"], "tender_related")
        self.assertEqual(collisions[0]["error_kind"], "")

    def test_shared_unmatched_bracket_is_reported(self):
        detector = CollusionDetector()
        content = "Warranty scope (includes equipment and onsite service"
        pages = [(1, content, detector.normalize(content))]

        errors = detector._find_shared_high_confidence_errors(pages, pages)

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error_kind"], "punctuation")
        self.assertIn("没有配对", errors[0]["desc"])

    def test_brackets_balanced_across_pages_are_not_reported(self):
        detector = CollusionDetector()
        pages = [
            (
                1,
                "Warranty scope (includes equipment",
                detector.normalize("Warranty scope (includes equipment"),
            ),
            (
                2,
                "and onsite service) is covered",
                detector.normalize("and onsite service) is covered"),
            ),
        ]

        errors = detector._find_shared_high_confidence_errors(pages, pages)

        self.assertFalse(errors)

    def test_malformed_number_separator_is_reported_once(self):
        detector = CollusionDetector()
        content = "1.. First malformed requirement"
        pages = [(1, content, detector.normalize(content))]

        errors = detector._find_shared_high_confidence_errors(pages, pages)

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error_kind"], "numbering")


if __name__ == "__main__":
    unittest.main()
