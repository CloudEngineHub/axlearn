# Copyright © 2024 Apple Inc.

# pylint: disable=protected-access
"""Unit tests for generator.py."""

import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from absl import flags

from axlearn.open_api import mock_utils

# Mock openai to avoid unnecessary dependency on openai library.
with mock_utils.mock_openai_package():
    # isort: off
    # pylint: disable=wrong-import-position
    from axlearn.open_api import common
    from axlearn.open_api.common import Evaluator, Generator
    from axlearn.open_api.evaluator import evaluate_from_file, evaluate_from_eval_set

# pylint: enable=wrong-import-position
# isort: one


@mock_utils.safe_mocks(mock_utils.mock_openai_package, mock_utils.mock_huggingface_hub_package)
class TestEvaluateFromFile(unittest.IsolatedAsyncioTestCase):
    """Unit test for evaluate_from_file."""

    def setUp(self):
        super().setUp()
        self.mock_responses = [
            {"response": "response1"},
            {"response": "response2"},
            {"response": "response3"},
        ]

        # Create a temporary file and write the mock responses to it.
        # pylint: disable-next=consider-using-with
        self.temp_file = tempfile.NamedTemporaryFile(mode="w+", delete=False)
        json.dump(self.mock_responses, self.temp_file)
        # Ensure data is written to file.
        self.temp_file.flush()
        self.temp_file_path = self.temp_file.name

    def tearDown(self):
        # Close and remove the temporary file
        self.temp_file.close()
        os.remove(self.temp_file_path)
        super().tearDown()

    @patch(
        f"{common.__name__}.Evaluator.evaluate",
        new_callable=MagicMock,
    )
    async def test_evaluate_from_file(self, mock_evaluate):
        fv = flags.FlagValues()
        Generator.define_flags(fv)
        Evaluator.define_flags(fv)
        fv.set_default("model", "test")
        fv.set_default("check_vllm_readiness", False)
        fv.set_default("metric_name", "tool_use_plan")
        fv.set_default("input_file", self.temp_file_path)
        fv.mark_as_parsed()

        # Call the method under test.
        evaluate_from_file(fv=fv)
        mock_evaluate.assert_called_once()

    @patch(
        "axlearn.open_api.generator.generate_from_requests",
        new_callable=AsyncMock,
    )
    @patch(
        "axlearn.open_api.eval_set.mmau.ToolUsePlan.load_requests",
        new_callable=MagicMock,
    )
    @patch(
        "axlearn.open_api.metrics.tool_use_plan.metric_fn",
        new_callable=MagicMock,
    )
    @pytest.mark.skip(reason="Flaky in CI.")  # TODO(guoli-yin): Fix and re-enable.
    def test_evaluate_from_eval_set(
        self, mock_metric_fn, mock_eval_set_fn, mock_generate_from_requests
    ):
        fv = flags.FlagValues()
        Generator.define_flags(fv)
        Evaluator.define_flags(fv)
        fv.set_default("model", "test")
        fv.set_default("check_vllm_readiness", False)
        fv.set_default("eval_set_name", "mmau_tool_use_plan")
        fv.mark_as_parsed()
        mock_eval_set_fn.return_value = [{"messages": [{"role": "user", "content": "prompt1"}]}]
        mock_generate_from_requests.return_value = [{"response": "resp1"}]
        mock_metric_fn.return_value = {"accuracy": 1.0}
        # Call the method under test.
        evaluate_from_eval_set(fv=fv)
        mock_metric_fn.assert_called_once()

    @patch(
        f"{common.__name__}.Evaluator.evaluate",
        new_callable=MagicMock,
    )
    # pylint: disable-next=unused-argument
    async def test_evaluate_from_file_no_metric(self, mock_evaluate):
        fv = flags.FlagValues()
        Generator.define_flags(fv)
        Evaluator.define_flags(fv)
        fv.set_default("model", "test")
        fv.set_default("check_vllm_readiness", False)
        fv.set_default("input_file", self.temp_file_path)
        fv.mark_as_parsed()

        # Test that ValueError is raised.
        with self.assertRaises(ValueError):
            # Call the method under test.
            evaluate_from_file(fv=fv)
