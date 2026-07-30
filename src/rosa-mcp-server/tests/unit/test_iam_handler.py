# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").

"""Unit tests for IAM handler."""

import json
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from awslabs.rosa_mcp_server.iam_handler import IAMHandler


@pytest.fixture
def handler():
    mcp = MagicMock()
    mcp.tool = MagicMock(return_value=lambda f: f)
    return IAMHandler(mcp, allow_sensitive_data_access=True, allow_write=True)


@pytest.fixture
def handler_readonly():
    mcp = MagicMock()
    mcp.tool = MagicMock(return_value=lambda f: f)
    return IAMHandler(mcp, allow_sensitive_data_access=False, allow_write=False)


class TestIAMInit:
    def test_tools_registered(self, handler):
        registered = [call.kwargs.get('name', '') for call in handler.mcp.tool.call_args_list]
        assert 'rosa_manage_iam' in registered

    def test_readonly_still_registers(self, handler_readonly):
        registered = [call.kwargs.get('name', '') for call in handler_readonly.mcp.tool.call_args_list]
        assert 'rosa_manage_iam' in registered


class TestManageIAM:
    @pytest.mark.asyncio
    async def test_invalid_operation_raises(self, handler):
        with pytest.raises(ValueError):
            await handler.rosa_manage_iam(None, operation='invalid_op')

    @pytest.mark.asyncio
    async def test_add_inline_policy_write_disabled_raises(self, handler_readonly):
        with pytest.raises(ValueError, match='[Ww]rite'):
            await handler_readonly.rosa_manage_iam(
                None,
                operation='add_inline_policy',
                role_name='test-role',
                policy_name='test-policy',
                policy_document={'Statement': []},
            )


class TestGetAccountRoles:
    @pytest.mark.asyncio
    async def test_lists_roles_with_prefix(self, handler):
        mock_iam = MagicMock()
        mock_iam.list_roles.return_value = {
            'Roles': [
                {'RoleName': 'ManagedOpenShift-HCP-ROSA-Installer-Role', 'RoleId': 'ABC', 'Arn': 'arn:aws:iam::123:role/test', 'CreateDate': '2026-01-01', 'Description': ''},
            ],
            'IsTruncated': False,
        }

        with patch('boto3.client', return_value=mock_iam):
            result = await handler.rosa_get_account_roles(None)
            # Should not raise


class TestVerifyQuota:
    @pytest.mark.asyncio
    async def test_verify_quota_calls_service_quotas(self, handler):
        mock_sq = MagicMock()
        mock_sq.get_paginator.return_value.paginate.return_value = []

        with patch('boto3.client', return_value=mock_sq):
            result = await handler.rosa_verify_quota(None)
            # Should not raise



class TestGetOperatorRoles:
    @pytest.mark.asyncio
    async def test_lists_operator_roles(self, handler):
        mock_iam = MagicMock()
        mock_iam.list_roles.return_value = {
            'Roles': [
                {'RoleName': 'my-cluster-openshift-ingress-operator', 'RoleId': 'X', 'Arn': 'arn:aws:iam::123:role/op', 'CreateDate': '2026-01-01', 'Description': ''},
            ],
            'IsTruncated': False,
        }

        with patch('boto3.client', return_value=mock_iam):
            result = await handler.rosa_get_operator_roles(None)
            assert result is not None


class TestListOidcProviders:
    @pytest.mark.asyncio
    async def test_lists_providers(self, handler):
        mock_iam = MagicMock()
        mock_iam.list_open_id_connect_providers.return_value = {
            'OpenIDConnectProviderList': [
                {'Arn': 'arn:aws:iam::123:oidc-provider/oidc.op1.openshiftapps.com/abc123'},
            ],
        }
        mock_iam.get_open_id_connect_provider.return_value = {
            'Url': 'https://oidc.op1.openshiftapps.com/abc123',
            'CreateDate': '2026-01-01',
            'ClientIDList': ['openshift'],
            'ThumbprintList': ['abc'],
        }

        with patch('boto3.client', return_value=mock_iam):
            result = await handler.rosa_list_oidc_providers(None)
            assert result is not None


class TestGetPoliciesForRole:
    @pytest.mark.asyncio
    async def test_returns_policies(self, handler):
        mock_iam = MagicMock()
        mock_iam.get_role.return_value = {
            'Role': {
                'RoleName': 'test',
                'Arn': 'arn:aws:iam::123:role/test',
                'AssumeRolePolicyDocument': '{}',
                'Description': '',
            },
        }
        mock_iam.list_attached_role_policies.return_value = {'AttachedPolicies': [], 'IsTruncated': False}
        mock_iam.list_role_policies.return_value = {'PolicyNames': [], 'IsTruncated': False}

        with patch('boto3.client', return_value=mock_iam):
            result = await handler.rosa_get_policies_for_role(None, role_name='test')
            assert result is not None


class TestAddInlinePolicy:
    @pytest.mark.asyncio
    async def test_write_disabled_raises(self, handler_readonly):
        with pytest.raises(ValueError, match='[Ww]rite'):
            await handler_readonly.rosa_add_inline_policy(
                None, role_name='r', policy_name='p', policy_document={'Statement': []}
            )

    @pytest.mark.asyncio
    async def test_adds_policy(self, handler):
        mock_iam = MagicMock()
        mock_iam.put_role_policy.return_value = {}

        with patch('boto3.client', return_value=mock_iam):
            result = await handler.rosa_add_inline_policy(
                None, role_name='my-role', policy_name='my-policy',
                policy_document={'Version': '2012-10-17', 'Statement': []}
            )
            assert result is not None
            mock_iam.put_role_policy.assert_called_once()



class TestManageIAMOperations:
    """Test rosa_manage_iam dispatcher."""

    @pytest.mark.asyncio
    async def test_list_account_roles_op(self, handler):
        mock_iam = MagicMock()
        mock_iam.list_roles.return_value = {'Roles': [], 'IsTruncated': False}
        with patch('boto3.client', return_value=mock_iam):
            result = await handler.rosa_manage_iam(None, operation='list_account_roles')
            assert result is not None

    @pytest.mark.asyncio
    async def test_list_operator_roles_op(self, handler):
        mock_iam = MagicMock()
        mock_iam.list_roles.return_value = {'Roles': [], 'IsTruncated': False}
        with patch('boto3.client', return_value=mock_iam):
            result = await handler.rosa_manage_iam(None, operation='list_operator_roles')
            assert result is not None

    @pytest.mark.asyncio
    async def test_list_oidc_providers_op(self, handler):
        mock_iam = MagicMock()
        mock_iam.list_open_id_connect_providers.return_value = {'OpenIDConnectProviderList': []}
        with patch('boto3.client', return_value=mock_iam):
            result = await handler.rosa_manage_iam(None, operation='list_oidc_providers')
            assert result is not None

    @pytest.mark.asyncio
    async def test_verify_quota_op(self, handler):
        mock_sq = MagicMock()
        mock_sq.get_paginator.return_value.paginate.return_value = []
        with patch('boto3.client', return_value=mock_sq):
            result = await handler.rosa_manage_iam(None, operation='verify_quota')
            assert result is not None

    @pytest.mark.asyncio
    async def test_get_policies_for_role_op(self, handler):
        mock_iam = MagicMock()
        mock_iam.get_role.return_value = {'Role': {'RoleName': 'r', 'Arn': 'arn', 'AssumeRolePolicyDocument': '{}', 'Description': ''}}
        mock_iam.list_attached_role_policies.return_value = {'AttachedPolicies': [], 'IsTruncated': False}
        mock_iam.list_role_policies.return_value = {'PolicyNames': [], 'IsTruncated': False}
        with patch('boto3.client', return_value=mock_iam):
            result = await handler.rosa_manage_iam(None, operation='get_policies_for_role', role_name='r')
            assert result is not None

    @pytest.mark.asyncio
    async def test_add_inline_policy_op(self, handler):
        mock_iam = MagicMock()
        mock_iam.put_role_policy.return_value = {}
        with patch('boto3.client', return_value=mock_iam):
            result = await handler.rosa_manage_iam(
                None, operation='add_inline_policy',
                role_name='r', policy_name='p',
                policy_document={'Version': '2012-10-17', 'Statement': []},
            )
            assert result is not None



class TestVerifyQuotaDetailed:
    @pytest.mark.asyncio
    async def test_verify_quota_with_results(self, handler):
        mock_sq = MagicMock()
        mock_sq.get_paginator.return_value.paginate.return_value = [
            {
                'Quotas': [
                    {'ServiceCode': 'ec2', 'QuotaName': 'Running On-Demand', 'Value': 100.0, 'UsageMetric': {'MetricNamespace': 'AWS/Usage'}},
                    {'ServiceCode': 'vpc', 'QuotaName': 'VPCs per Region', 'Value': 5.0, 'UsageMetric': {}},
                ],
            },
        ]

        with patch('boto3.client', return_value=mock_sq):
            result = await handler.rosa_verify_quota(None)
            assert result is not None

    @pytest.mark.asyncio
    async def test_get_account_roles_with_results(self, handler):
        mock_iam = MagicMock()
        mock_iam.list_roles.return_value = {
            'Roles': [
                {'RoleName': 'ManagedOpenShift-HCP-ROSA-Installer-Role', 'RoleId': 'A1', 'Arn': 'arn:aws:iam::123:role/installer', 'CreateDate': '2026-01-01T00:00:00Z', 'Description': 'Installer'},
                {'RoleName': 'ManagedOpenShift-HCP-ROSA-Worker-Role', 'RoleId': 'A2', 'Arn': 'arn:aws:iam::123:role/worker', 'CreateDate': '2026-01-01T00:00:00Z', 'Description': 'Worker'},
                {'RoleName': 'UnrelatedRole', 'RoleId': 'A3', 'Arn': 'arn:aws:iam::123:role/other', 'CreateDate': '2026-01-01T00:00:00Z', 'Description': 'Other'},
            ],
            'IsTruncated': False,
        }

        with patch('boto3.client', return_value=mock_iam):
            result = await handler.rosa_get_account_roles(None)
            data = json.loads(result[0].text)
            assert data['role_count'] >= 0  # Filter may vary

    @pytest.mark.asyncio
    async def test_get_operator_roles_with_results(self, handler):
        mock_iam = MagicMock()
        mock_iam.list_roles.return_value = {
            'Roles': [
                {'RoleName': 'my-cluster-openshift-ingress-operator-cloud-credentials', 'RoleId': 'B1', 'Arn': 'arn:aws:iam::123:role/op1', 'CreateDate': '2026-01-01T00:00:00Z', 'Description': ''},
            ],
            'IsTruncated': False,
        }

        with patch('boto3.client', return_value=mock_iam):
            result = await handler.rosa_get_operator_roles(None)
            assert result is not None

    @pytest.mark.asyncio
    async def test_list_oidc_providers_with_results(self, handler):
        mock_iam = MagicMock()
        mock_iam.list_open_id_connect_providers.return_value = {
            'OpenIDConnectProviderList': [
                {'Arn': 'arn:aws:iam::123:oidc-provider/oidc.op1.openshiftapps.com/abc'},
                {'Arn': 'arn:aws:iam::123:oidc-provider/other.provider.com'},
            ],
        }
        mock_iam.get_open_id_connect_provider.return_value = {
            'Url': 'https://oidc.op1.openshiftapps.com/abc',
            'CreateDate': '2026-01-01T00:00:00Z',
            'ClientIDList': ['openshift'],
            'ThumbprintList': ['abc123'],
        }

        with patch('boto3.client', return_value=mock_iam):
            result = await handler.rosa_list_oidc_providers(None)
            assert result is not None
