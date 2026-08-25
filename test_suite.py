"""
test_suite.py - Unit tests for monday.com Founder BI Agent
Validates English date normalization, categorical cleaning, data quality auditing, and agent tool logic.
"""

import unittest
from datetime import datetime
from normalize import (
    parse_full_date,
    parse_month_only_field,
    normalize_numeric,
    normalize_sector,
    normalize_deal_status,
    normalize_billing_status,
    normalize_invoice_status,
    normalize_wo_status,
    normalize_deals_data,
    normalize_work_orders_data,
    DataQualityReport
)


class TestNormalization(unittest.TestCase):

    def test_english_dates(self):
        # English full date
        iso, month = parse_full_date("March 3rd 2026")
        self.assertEqual(iso, "2026-03-03")
        self.assertEqual(month, "2026-03")

        # English abbreviation
        iso, month = parse_full_date("15 Feb 2026")
        self.assertEqual(iso, "2026-02-15")
        self.assertEqual(month, "2026-02")

        # Slash date
        iso, month = parse_full_date("03/03/26")
        self.assertIsNotNone(iso)
        self.assertEqual(iso, "2026-03-03")

        # ISO date
        iso, month = parse_full_date("2026-11-20")
        self.assertEqual(iso, "2026-11-20")
        self.assertEqual(month, "2026-11")

        # Missing / null
        iso, month = parse_full_date(None)
        self.assertIsNone(iso)
        self.assertIsNone(month)

        iso, month = parse_full_date("N/A")
        self.assertIsNone(iso)
        self.assertIsNone(month)

    def test_month_only_field_parsing(self):
        self.assertEqual(parse_month_only_field("Jan"), f"{datetime.now().year}-01")
        self.assertEqual(parse_month_only_field("Feb 2026"), "2026-02")
        self.assertEqual(parse_month_only_field("2025-08"), "2025-08")
        self.assertEqual(parse_month_only_field("03/2026"), "2026-03")
        self.assertIsNone(parse_month_only_field(None))
        self.assertIsNone(parse_month_only_field("-"))

    def test_numeric_parsing(self):
        self.assertEqual(normalize_numeric("₹50,000"), 50000.0)
        self.assertEqual(normalize_numeric("50k"), 50000.0)
        self.assertEqual(normalize_numeric("1.5M"), 1500000.0)
        self.assertEqual(normalize_numeric("120000 INR"), 120000.0)
        self.assertEqual(normalize_numeric(4500), 4500.0)
        self.assertIsNone(normalize_numeric("None"))
        self.assertIsNone(normalize_numeric(None))

    def test_sector_normalization(self):
        self.assertEqual(normalize_sector("Mining"), "Mining")
        self.assertEqual(normalize_sector("Renewables"), "Renewables")
        self.assertEqual(normalize_sector("Railways"), "Railways")
        self.assertEqual(normalize_sector("Powerline"), "Powerline")
        self.assertEqual(normalize_sector("Construction"), "Construction")
        self.assertEqual(normalize_sector(None), "Unspecified")

    def test_deal_status_and_garbage_filtering(self):
        self.assertEqual(normalize_deal_status("Won"), "Won")
        self.assertEqual(normalize_deal_status("Dead"), "Dead")
        self.assertEqual(normalize_deal_status("Open"), "Open")
        self.assertEqual(normalize_deal_status("On Hold"), "On Hold")
        # Garbage header row value returns None
        self.assertIsNone(normalize_deal_status("Deal Status"))

    def test_billing_and_invoice_status_normalization(self):
        # Case normalization
        self.assertEqual(normalize_billing_status("BIlled"), "Billed")
        self.assertEqual(normalize_billing_status("not billed yet"), "Not Billed")

        # Invoice Status visit-number bucketing
        norm_inv, was_bucketed = normalize_invoice_status("Billed- Visit 7")
        self.assertEqual(norm_inv, "Partially Billed")
        self.assertTrue(was_bucketed)

        norm_inv, was_bucketed = normalize_invoice_status("Fully Billed")
        self.assertEqual(norm_inv, "Fully Billed")
        self.assertFalse(was_bucketed)

    def test_wo_status_unknown(self):
        self.assertEqual(normalize_wo_status("Closed"), "Closed")
        self.assertEqual(normalize_wo_status("Open"), "Open")
        self.assertEqual(normalize_wo_status(None), "Unknown")
        self.assertEqual(normalize_wo_status(""), "Unknown")

    def test_deals_normalization_pipeline(self):
        mock_meta = {
            "name": "Deal funnel Data",
            "columns": [
                {"id": "c1", "title": "Deal Name", "type": "name"},
                {"id": "c2", "title": "Masked Deal value", "type": "numbers"},
                {"id": "c3", "title": "Deal Status", "type": "status"},
                {"id": "c4", "title": "Sector/service", "type": "dropdown"},
                {"id": "c5", "title": "Close Date (A)", "type": "date"}
            ]
        }
        mock_raw_items = [
            {
                "id": "1",
                "name": "Solar Project Alpha",
                "column_values": [
                    {"id": "c1", "text": "Solar Project Alpha"},
                    {"id": "c2", "text": "150000"},
                    {"id": "c3", "text": "Won"},
                    {"id": "c4", "text": "Renewables"},
                    {"id": "c5", "text": "15 Mar 2026"}
                ]
            },
            {
                "id": "2",
                "name": "Garbage Header Row",
                "column_values": [
                    {"id": "c1", "text": "Deal Name"},
                    {"id": "c2", "text": None},
                    {"id": "c3", "text": "Deal Status"},
                    {"id": "c4", "text": None},
                    {"id": "c5", "text": None}
                ]
            }
        ]

        items, report = normalize_deals_data(mock_meta, mock_raw_items)
        self.assertEqual(len(items), 1)  # Garbage row successfully filtered
        self.assertEqual(items[0]["sector"], "Renewables")
        self.assertEqual(items[0]["deal_status"], "Won")
        self.assertEqual(items[0]["deal_value"], 150000.0)
        self.assertEqual(items[0]["close_month"], "2026-03")


class TestAgentAndGroqIntegration(unittest.TestCase):

    def test_tools_schema_validity(self):
        from agent import TOOLS_SCHEMA
        self.assertEqual(len(TOOLS_SCHEMA), 4)
        tool_names = [t["function"]["name"] for t in TOOLS_SCHEMA]
        self.assertIn("fetch_deals", tool_names)
        self.assertIn("fetch_work_orders", tool_names)
        self.assertIn("get_data_quality_report", tool_names)
        self.assertIn("analyze_cross_board_performance", tool_names)

    def test_agent_initialization(self):
        from agent import FounderBIAgent
        agent = FounderBIAgent()
        self.assertIn(agent.provider, ["groq", "unconfigured"])
        self.assertIsNotNone(agent.model_name)

    def test_groq_key_failover_mechanism(self):
        from agent import FounderBIAgent
        from unittest.mock import patch, MagicMock

        agent = FounderBIAgent()
        agent.groq_keys = ["primary_fake_key", "secondary_fake_key"]
        agent.current_key_index = 0

        # Simulate first key failing with rate limit error, second key succeeding
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="Fallback success", tool_calls=None))]

        call_count = 0
        def fake_create(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Rate limit exceeded on primary key")
            return mock_response

        with patch("groq.Groq") as mock_groq_cls:
            mock_client = MagicMock()
            mock_client.chat.completions.create.side_effect = fake_create
            mock_groq_cls.return_value = mock_client

            resp = agent._call_chat_completion(messages=[{"role": "user", "content": "test"}])
            self.assertEqual(resp.choices[0].message.content, "Fallback success")
            self.assertEqual(agent.current_key_index, 1)  # Rotated to fallback key index


if __name__ == "__main__":
    unittest.main()
