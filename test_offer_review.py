import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.kwork_filter import generate_offer_claude


BRIEF = "Сопоставить прайс-листы и выгрузить результаты в XLSX."
TEXT = (
    "Сверю позиции из двух прайс-листов по наименованию и фасовке. "
    "По спорным совпадениям вы сможете выбрать товар вручную. "
    "В результате получите файл XLSX с ценами обоих поставщиков и разницей по каждой позиции."
)


def response(payload, stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=json.dumps(payload, ensure_ascii=False))],
    )


def draft(text=TEXT):
    return response({"text": text, "price": 5000, "days": 3, "stages": [], "note": "", "decline": False})


def verdict(result="pass", issue=""):
    return response({"verdict": result, "issue": issue})


class OfferReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch("app.kwork_filter.anthropic_client")
        factory = patcher.start()
        self.addCleanup(patcher.stop)
        client = factory.return_value.__aenter__.return_value
        self.generate = client.beta.messages.create = AsyncMock()
        self.review = client.messages.create = AsyncMock()

    async def offer(self, description=BRIEF, **options):
        return await generate_offer_claude("Прайсы", description, "5000 ₽", "test-key", **options)

    async def test_full_brief_and_exact_phrase_reach_both_calls(self):
        phrase = "«Прайс—готов!»"
        description = (BRIEF + "\n") * 100 + "Начните отклик точно с фразы " + phrase
        self.assertGreater(description.index(phrase), 4000)
        self.generate.return_value = draft(phrase + " " + TEXT)
        self.review.return_value = verdict()

        result = await self.offer(description)

        self.generate.assert_awaited_once()
        self.review.assert_awaited_once()
        brief = json.loads(self.generate.await_args.kwargs["messages"][0]["content"])
        checked = json.loads(self.review.await_args.kwargs["messages"][0]["content"])
        self.assertEqual(brief["description"], description)
        self.assertEqual(checked["description"], description)
        self.assertEqual(checked["text"], result["text"])
        self.assertTrue(result["text"].startswith(phrase))

    async def test_revision_replaces_draft_and_is_reviewed_again(self):
        issue = "Ответьте, передадите ли вы исходники."
        corrected = TEXT + " Передам исходники."
        self.generate.side_effect = [draft(), draft(corrected)]
        self.review.side_effect = [verdict("revise", issue), verdict()]

        result = await self.offer(BRIEF + " Передадите ли вы исходники?")

        revision = self.generate.await_args_list[1].kwargs["messages"][0]["content"]
        self.assertIn(TEXT, revision)
        self.assertIn(issue, revision)
        reviewed = [json.loads(call.kwargs["messages"][0]["content"])["text"]
                    for call in self.review.await_args_list]
        self.assertEqual(reviewed, [TEXT, corrected])
        self.assertEqual(result["text"], corrected)

    async def test_two_revisions_withhold_text(self):
        self.generate.return_value = draft()
        self.review.return_value = verdict("revise", "Не указан формат результата.")

        result = await self.offer()

        self.assertEqual(self.review.await_count, 2)
        self.assertTrue(result["decline"])
        self.assertEqual(result["text"], "")

    async def test_review_refusal_or_error_withholds_text(self):
        self.generate.return_value = draft()
        for failure in (response({"verdict": "pass", "issue": ""}, "refusal"), RuntimeError("offline")):
            with self.subTest(failure=failure):
                self.review.side_effect = [failure, failure]
                self.review.reset_mock()
                result = await self.offer()
                self.assertEqual(self.review.await_count, 2)
                self.assertFalse((result or {}).get("text"))

    async def test_minimum_length(self):
        self.review.return_value = verdict()
        for length in (149, 150):
            with self.subTest(length=length):
                text = "а" * length
                self.generate.return_value = draft(text)
                self.review.reset_mock()
                result = await self.offer(max_retries=0)
                if length == 149:
                    self.review.assert_not_awaited()
                    self.assertTrue(result["decline"])
                    self.assertEqual(result["text"], "")
                else:
                    self.review.assert_awaited_once()
                    self.assertEqual(result["text"], text)
