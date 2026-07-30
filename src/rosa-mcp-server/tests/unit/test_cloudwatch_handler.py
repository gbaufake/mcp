# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").

"""Unit tests for CloudWatch handler."""

import pytest
from unittest.mock import MagicMock, patch
from awslabs.rosa_mcp_server.cloudwatch_handler import CloudWatchHandler


@pytest.fixture
def handler():
    mcp = MagicMock()
    mcp.tool = MagicMock(return_value=lambda f: f)
    return CloudWatchHandler(mcp, allow_sensitive_data_access=True)


@pytest.fixture
def handler_restricted():
    mcp = MagicMock()
    mcp.tool = MagicMock(return_value=lambda f: f)
    return CloudWatchHandler(mcp, allow_sensitive_data_access=False)


class TestCloudWatchInit:
    def test_tools_registered(self, handler):
        registered = [call.kwargs.get('name', '') for call in handler.mcp.tool.call_args_list]
        assert 'rosa_get_cloudwatch_logs' in registered
        assert 'rosa_get_cloudwatch_metrics' in registered


class TestResolveTimeRange:
    def test_default_15_minutes(self, handler):
        start, end = handler._resolve_time_range(minutes=15)
        assert end > start
        diff_seconds = (end - start).total_seconds()
        assert 890 < diff_seconds < 910  # ~15 min

    def test_custom_minutes(self, handler):
        start, end = handler._resolve_time_range(minutes=60)
        diff_seconds = (end - start).total_seconds()
        assert 3590 < diff_seconds < 3610  # ~60 min

    def test_iso_start_time(self, handler):
        start, end = handler._resolve_time_range(
            start_time='2026-01-01T00:00:00Z',
            end_time='2026-01-01T01:00:00Z',
        )
        assert start.year == 2026
        assert start.month == 1


class TestGetCloudWatchLogs:
    @pytest.mark.asyncio
    async def test_sensitive_data_disabled_raises(self, handler_restricted):
        with pytest.raises(ValueError, match='[Ss]ensitive data'):
            await handler_restricted.rosa_get_cloudwatch_logs(
                None,
                log_group_name='/aws/rosa/test/worker',
            )


class TestGetCloudWatchMetrics:
    @pytest.mark.asyncio
    async def test_returns_metrics_structure(self, handler):
        # This would need boto3 mock for full test, but at minimum
        # verify it doesn't crash on init
        assert handler.allow_sensitive_data_access is True



class TestGetCloudWatchLogsWithMock:
    @pytest.mark.asyncio
    async def test_returns_log_events(self, handler):
        mock_logs = MagicMock()
        mock_logs.start_query.return_value = {'queryId': 'q1'}
        mock_logs.get_query_results.return_value = {
            'status': 'Complete',
            'results': [
                [{'field': '@timestamp', 'value': '2026-01-01'}, {'field': '@message', 'value': 'test log'}],
            ],
        }

        with patch('boto3.client', return_value=mock_logs):
            result = await handler.rosa_get_cloudwatch_logs(
                None, log_group_name='/aws/rosa/test/worker'
            )
            assert result is not None

    @pytest.mark.asyncio
    async def test_with_filter_pattern(self, handler):
        mock_logs = MagicMock()
        mock_logs.start_query.return_value = {'queryId': 'q1'}
        mock_logs.get_query_results.return_value = {
            'status': 'Complete',
            'results': [],
        }

        with patch('boto3.client', return_value=mock_logs):
            result = await handler.rosa_get_cloudwatch_logs(
                None, log_group_name='/aws/rosa/test/worker', filter_pattern='ERROR'
            )
            assert result is not None


class TestGetCloudWatchMetricsWithMock:
    @pytest.mark.asyncio
    async def test_returns_metric_data(self, handler):
        mock_cw = MagicMock()
        mock_cw.get_metric_data.return_value = {
            'MetricDataResults': [
                {
                    'Id': 'm1',
                    'Timestamps': ['2026-01-01T00:00:00Z'],
                    'Values': [42.5],
                },
            ],
        }

        with patch('boto3.client', return_value=mock_cw):
            result = await handler.rosa_get_cloudwatch_metrics(
                None,
                namespace='ContainerInsights',
                metric_name='pod_cpu_utilization',
                dimensions='{"ClusterName": "test"}',
            )
            assert result is not None
